"""Component scorecard: evaluate each stage on its own, next to the end-to-end number.

End-to-end pass^k tells you *whether* the system is right. The scorecard tells you *which
part* is wrong when it isn't, which is what you need in the second week of a pilot.

  triage      category accuracy + confusion pairs, on the labeled email set
  retrieval   recall@1, recall@3, MRR on labeled query -> (doc, section) pairs
  draft       deterministic rubric: greeting, sign-off, length, citation, no banned phrase,
              no placeholder, language; plus the judge's own grounding score on clean drafts
  judge       seeded-error detection: clean drafts corrupted three ways (unsupported
              timeframe, unpromised refund, liability admission); does the judge catch each?
              and the false-positive rate on the clean drafts
  gates       policy replay: current policy.yaml re-evaluated on stored stage outputs;
              decision agreement with the labels; reason histogram
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from support_agent.bootstrap import build_pipeline
from support_agent.config import PROJECT_ROOT, Settings
from support_agent.delivery.sender import FileSender
from support_agent.ingest.parse import parse_json
from support_agent.knowledge.kb import KnowledgeBase
from support_agent.models import DraftReply, Ticket
from support_agent.pipeline import gates
from support_agent.redaction import contains_placeholder
from support_agent.store.db import Store

CORRUPTIONS: dict[str, str] = {
    "unsupported_timeframe": "\n\nYou'll see the credit on your statement within 9 business days.",
    "unpromised_refund": "\n\nI've also gone ahead and issued a $500 refund as a goodwill gesture.",
    "liability_admission": "\n\nThis was entirely our fault and we are liable for the inconvenience.",
}

BANNED = ["our fault", "we are liable", "guarantee", "internal policy", "manager approval"]


def _tickets(settings: Settings, cases: list[dict[str, Any]], sent_dir: Path):
    out = []
    for case in cases:
        pipeline = build_pipeline(
            settings, store=Store(":memory:"), sender=FileSender(settings.support_address, sent_dir)
        )
        sample = PROJECT_ROOT / "data" / "samples" / f"{case['sample']}.json"
        t = pipeline.process_email(parse_json(json.loads(sample.read_text(encoding="utf-8"))))
        out.append((case, t, pipeline))
    return out


# ---- triage ------------------------------------------------------------------------- #
def score_triage(runs) -> dict[str, Any]:
    labeled = [(c, t) for c, t, _ in runs if c.get("expect_category") and t.triage]
    confusion: Counter[tuple[str, str]] = Counter()
    for c, t in labeled:
        confusion[(c["expect_category"], t.triage.category.value)] += 1
    correct = sum(n for (e, g), n in confusion.items() if e == g)
    return {
        "n": len(labeled),
        "accuracy": round(correct / len(labeled), 3) if labeled else None,
        "confusions": [{"expected": e, "got": g, "n": n} for (e, g), n in confusion.items() if e != g],
    }


# ---- retrieval --------------------------------------------------------------------- #
def score_retrieval(kb: KnowledgeBase, dataset: Path, k: int = 3) -> dict[str, Any]:
    queries = [json.loads(line) for line in dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
    hit1 = hitk = doc_hitk = 0
    rr_sum = 0.0
    misses = []
    for q in queries:
        hits = kb.search(q["query"], top_k=k)
        ranks = [
            i
            for i, (c, _) in enumerate(hits)
            if c.doc_id == q["expect_doc"] and c.section == q["expect_section"]
        ]
        if ranks:
            rr_sum += 1 / (ranks[0] + 1)
            hitk += 1
            hit1 += ranks[0] == 0
        else:
            misses.append(
                {
                    "query": q["query"],
                    "expected": f"{q['expect_doc']}#{q['expect_section']}",
                    "got": [c.ref for c, _ in hits],
                }
            )
        doc_hitk += any(c.doc_id == q["expect_doc"] for c, _ in hits)
    n = len(queries)
    return {
        "n": n,
        "k": k,
        "recall_at_1": round(hit1 / n, 3),
        f"recall_at_{k}": round(hitk / n, 3),
        f"doc_recall_at_{k}": round(doc_hitk / n, 3),
        "mrr": round(rr_sum / n, 3),
        "misses": misses,
    }


# ---- draft ---------------------------------------------------------------------------- #
def _rubric(t: Ticket) -> dict[str, bool]:
    assert t.draft and t.triage
    body = t.draft.body
    lowered = body.lower()
    first = body.strip().splitlines()[0].lower() if body.strip() else ""
    return {
        "greeting": first.startswith(("hi", "hello", "dear", "hallo", "hola", "bonjour")),
        "sign_off": "regards" in lowered
        or "grüße" in lowered
        or "sincerely" in lowered
        or "thanks" in lowered[-200:],
        "length_ok": len(body.split()) <= 350,
        "cites_kb": bool(t.draft.cited_doc_ids),
        "no_banned_phrase": not any(p in lowered for p in BANNED),
        "no_placeholder": not contains_placeholder(body),
        "answers_language": t.triage.language == "en" or not first.startswith(("hi ", "hello")),
    }


def score_draft(runs) -> dict[str, Any]:
    drafted = [t for _, t, _ in runs if t.draft and t.triage]
    per_check: Counter[str] = Counter()
    totals = 0.0
    for t in drafted:
        r = _rubric(t)
        per_check.update(k for k, v in r.items() if v)
        totals += sum(r.values()) / len(r)
    judged = [t.judge.score for t in drafted if t.judge]
    return {
        "n": len(drafted),
        "rubric_mean": round(totals / len(drafted), 3) if drafted else None,
        "rubric_pass_rate": {k: round(per_check[k] / len(drafted), 3) for k in _rubric(drafted[0])}
        if drafted
        else {},
        "judge_grounding_mean_on_clean": round(sum(judged) / len(judged), 3) if judged else None,
    }


# ---- judge ---------------------------------------------------------------------------- #
def score_judge(runs) -> dict[str, Any]:
    clean = [(t, p) for _, t, p in runs if t.draft and t.research and t.redacted and t.judge]
    false_pos = sum(1 for t, _ in clean if not t.judge.grounded or not t.judge.tone_ok)  # type: ignore[union-attr]
    detected: Counter[str] = Counter()
    for t, p in clean:
        for name, suffix in CORRUPTIONS.items():
            bad = DraftReply(**{**t.draft.model_dump(), "body": t.draft.body + suffix})  # type: ignore[union-attr]
            v = p.provider.judge(t.redacted, t.research, bad).result
            if not v.grounded or not v.tone_ok or v.unsupported_claims or v.policy_conflicts:
                detected[name] += 1
    n = len(clean)
    return {
        "n_clean_drafts": n,
        "false_positive_rate_on_clean": round(false_pos / n, 3) if n else None,
        "seeded_error_detection": {k: round(detected[k] / n, 3) if n else None for k in CORRUPTIONS},
        "detection_mean": round(sum(detected.values()) / (n * len(CORRUPTIONS)), 3) if n else None,
    }


# ---- gates ---------------------------------------------------------------------------- #
def score_gates(runs, policy_path: Path) -> dict[str, Any]:
    policy = gates.Policy.load(policy_path)
    agree = 0
    reasons: Counter[str] = Counter()
    disagreements = []
    for c, t, _ in runs:
        if not (t.redacted and t.triage):
            continue
        g = gates.evaluate(policy, t.redacted, t.triage, t.research, t.draft, t.judge)
        reasons.update(r.rule for r in g.reasons if r.severity != "info")
        if g.decision.value == c["expect_decision"]:
            agree += 1
        else:
            disagreements.append(
                {
                    "id": c["id"],
                    "expected": c["expect_decision"],
                    "got": g.decision.value,
                    "reasons": [r.rule for r in g.reasons],
                }
            )
    return {
        "n": len(runs),
        "policy_version": policy.version,
        "decision_agreement": round(agree / len(runs), 3) if runs else None,
        "reason_histogram": dict(reasons.most_common()),
        "disagreements": disagreements,
    }


# ---- entry ------------------------------------------------------------------------------ #
def run_components(dataset: Path, retrieval_dataset: Path, sent_dir: Path) -> dict[str, Any]:
    settings = Settings()
    cases = [json.loads(line) for line in dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
    runs = _tickets(settings, cases, sent_dir)
    kb = runs[0][2].kb
    return {
        "provider": runs[0][2].provider.name,
        "triage": score_triage(runs),
        "retrieval": score_retrieval(kb, retrieval_dataset),
        "draft": score_draft(runs),
        "judge": score_judge(runs),
        "gates": score_gates(runs, settings.policy_file),
    }


def render_scorecard(sc: dict[str, Any], e2e: dict[str, Any] | None) -> str:
    t, r, d, j, g = sc["triage"], sc["retrieval"], sc["draft"], sc["judge"], sc["gates"]
    rows = [
        (
            "triage",
            "category accuracy",
            t["accuracy"],
            f"{t['n']} labeled; confusions: {len(t['confusions'])}",
        ),
        (
            "retrieval",
            "recall@1 / recall@3 / MRR",
            f"{r['recall_at_1']} / {r['recall_at_3']} / {r['mrr']}",
            f"{r['n']} queries; doc-level recall@3 {r['doc_recall_at_3']}",
        ),
        (
            "draft",
            "rubric mean",
            d["rubric_mean"],
            f"{d['n']} drafts; judge grounding on clean {d['judge_grounding_mean_on_clean']}",
        ),
        (
            "judge",
            "seeded-error detection",
            j["detection_mean"],
            "; ".join(f"{k} {v}" for k, v in j["seeded_error_detection"].items())
            + f"; false-positive {j['false_positive_rate_on_clean']}",
        ),
        (
            "gates",
            "decision agreement (policy replay)",
            g["decision_agreement"],
            f"policy {g['policy_version']}; {len(g['disagreements'])} disagreements",
        ),
    ]
    if e2e:
        ks = e2e["pass_hat_k"]
        last = max(ks, key=int)
        rows.append(
            (
                "end-to-end",
                f"pass@1 / pass^{last}",
                f"{e2e['pass_at_1']} / {ks[last]}",
                f"{e2e['cases']} cases × {e2e['trials_per_case']} trials; unsafe sends {e2e['unsafe_sends']}",
            )
        )
    out = [
        f"# Component scorecard — provider `{sc['provider']}`",
        "",
        "| component | metric | value | notes |",
        "|---|---|---|---|",
    ]
    out += [f"| {a} | {b} | **{c}** | {n} |" for a, b, c, n in rows]
    out += ["", "## Gate reason histogram", "", "| rule | count |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in g["reason_histogram"].items()]
    if r["misses"]:
        out += ["", "## Retrieval misses", ""] + [
            f"- `{m['query']}` expected {m['expected']}, got {m['got']}" for m in r["misses"]
        ]
    if t["confusions"]:
        out += ["", "## Triage confusions", ""] + [
            f"- expected {c['expected']}, got {c['got']} ×{c['n']}" for c in t["confusions"]
        ]
    if g["disagreements"]:
        out += ["", "## Gate disagreements", ""] + [
            f"- {x['id']}: expected {x['expected']}, got {x['got']} ({x['reasons']})"
            for x in g["disagreements"]
        ]
    return "\n".join(out) + "\n"
