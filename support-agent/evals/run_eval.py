"""Eval harness: replay a labeled email set through the full pipeline, n trials per case.

Per case, a trial *passes* when every labeled check holds: gate decision (always), triage
category, proposed action set, and must/must-not phrases in the reply (when labeled).

Two headline numbers, following tau-bench's framing (Yao et al., 2024):

  pass@1   mean over cases of (successes / trials). "How often is one attempt right?"
  pass^k   mean over cases of C(c, k) / C(n, k). "How often are ALL k attempts right?"
           This is the reliability number. An agent at pass@1 = 0.9 with pass^4 = 0.6
           will embarrass you on the fourth customer with the same problem.

Also reported: unsafe sends (auto_send where a human was expected; any > 0 fails the run),
failed tickets, cost and latency per case. Deterministic on the fake provider (pass^k ==
pass@1 by construction); the numbers only mean something on the live provider:

    SA_LLM_PROVIDER=anthropic support-agent eval --trials 4
"""

from __future__ import annotations

import json
import sys
import time
from math import comb
from pathlib import Path
from typing import Any

from support_agent.bootstrap import build_pipeline
from support_agent.config import PROJECT_ROOT, Settings
from support_agent.delivery.sender import FileSender
from support_agent.ingest.parse import parse_json
from support_agent.models import GateDecision
from support_agent.store.db import Store

HUMAN_DECISIONS = {GateDecision.needs_approval.value, GateDecision.escalate.value}


def run_trial(pipeline, case: dict[str, Any]) -> dict[str, Any]:
    sample = PROJECT_ROOT / "data" / "samples" / f"{case['sample']}.json"
    email = parse_json(json.loads(sample.read_text(encoding="utf-8")))
    t0 = time.perf_counter()
    t = pipeline.process_email(email)
    elapsed = time.perf_counter() - t0
    decision = t.gate.decision.value if t.gate else None
    category = t.triage.category.value if t.triage else None
    actions = (
        sorted({a.type.value for a in t.draft.proposed_actions if a.type.value != "none"}) if t.draft else []
    )
    body = (t.draft.body if t.draft else "") or ""
    checks: dict[str, bool] = {"decision": decision == case["expect_decision"]}
    if case.get("expect_category") is not None:
        checks["triage"] = category == case["expect_category"]
    if case.get("expect_actions") is not None:
        checks["actions"] = actions == sorted(case["expect_actions"])
    if case.get("must_include") or case.get("must_not_include"):
        lowered = body.lower()
        checks["content"] = all(p.lower() in lowered for p in case.get("must_include", [])) and not any(
            p.lower() in lowered for p in case.get("must_not_include", [])
        )
    return {
        "status": t.status.value,
        "error": t.error,
        "category": category,
        "decision": decision,
        "actions": actions,
        "gate_reasons": [r.rule for r in t.gate.reasons] if t.gate else [],
        "cost_usd": t.total_cost_usd,
        "latency_s": round(elapsed, 2),
        "checks": checks,
        "passed": all(checks.values()) and t.status.value != "failed",
        "unsafe_send": decision == "auto_send" and case["expect_decision"] in HUMAN_DECISIONS,
    }


def pass_hat_k(c: int, n: int, k: int) -> float:
    """Unbiased estimate of P(all k i.i.d. trials succeed) from n trials with c successes."""
    return comb(c, k) / comb(n, k) if n >= k else float("nan")


def run(dataset: Path, trials: int, sent_dir: Path) -> dict[str, Any]:
    settings = Settings()
    cases = [json.loads(line) for line in dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
    results: list[dict[str, Any]] = []
    provider_name, policy_version = "", ""
    for case in cases:
        runs = []
        for _ in range(trials):
            # A fresh store per trial: ticket ids are idempotent, and we want n independent runs.
            pipeline = build_pipeline(
                settings, store=Store(":memory:"), sender=FileSender(settings.support_address, sent_dir)
            )
            provider_name, policy_version = pipeline.provider.name, pipeline.policy.version
            runs.append(run_trial(pipeline, case))
        c = sum(r["passed"] for r in runs)
        results.append(
            {
                "id": case["id"],
                "sample": case["sample"],
                "expect_decision": case["expect_decision"],
                "expect_category": case.get("expect_category"),
                "trials": trials,
                "successes": c,
                "pass_at_1": round(c / trials, 3),
                "pass_hat_k": {str(k): round(pass_hat_k(c, trials, k), 3) for k in range(1, trials + 1)},
                "unsafe_sends": sum(r["unsafe_send"] for r in runs),
                "failed_tickets": sum(r["status"] == "failed" for r in runs),
                "decisions": [r["decision"] for r in runs],
                "mean_cost_usd": round(sum(r["cost_usd"] for r in runs) / trials, 5),
                "mean_latency_s": round(sum(r["latency_s"] for r in runs) / trials, 2),
                "runs": runs,
            }
        )

    def mean(key: str) -> float:
        return round(sum(r[key] for r in results) / len(results), 3)

    summary = {
        "provider": provider_name,
        "policy_version": policy_version,
        "cases": len(results),
        "trials_per_case": trials,
        "pass_at_1": mean("pass_at_1"),
        "pass_hat_k": {
            str(k): round(sum(r["pass_hat_k"][str(k)] for r in results) / len(results), 3)
            for k in range(1, trials + 1)
        },
        "decision_accuracy": round(
            sum(rr["checks"]["decision"] for r in results for rr in r["runs"]) / (len(results) * trials), 3
        ),
        "unsafe_sends": sum(r["unsafe_sends"] for r in results),
        "failed_tickets": sum(r["failed_tickets"] for r in results),
        "total_cost_usd": round(sum(r["mean_cost_usd"] * trials for r in results), 4),
        "mean_latency_s": mean("mean_latency_s"),
    }
    return {"summary": summary, "cases": results}


def render_markdown(report: dict[str, Any]) -> str:
    s, cases = report["summary"], report["cases"]
    ks = list(s["pass_hat_k"])
    out = [
        f"# Eval report — provider `{s['provider']}`, policy `{s['policy_version']}`",
        "",
        f"{s['cases']} cases × {s['trials_per_case']} trials. "
        f"Unsafe sends: **{s['unsafe_sends']}**. Failed tickets: {s['failed_tickets']}. "
        f"Total cost ${s['total_cost_usd']:.4f}, mean latency {s['mean_latency_s']}s.",
        "",
        "| metric | value |",
        "|---|---|",
        f"| pass@1 | **{s['pass_at_1']:.3f}** |",
        *[f"| pass^{k} | **{s['pass_hat_k'][k]:.3f}** |" for k in ks if k != "1"],
        f"| decision accuracy | {s['decision_accuracy']:.3f} |",
        "",
        "## Per case",
        "",
        "| id | sample | expected | pass@1 | "
        + " | ".join(f"pass^{k}" for k in ks if k != "1")
        + " | decisions seen | unsafe | cost |",
        "|---|---|---|---|" + "---|" * (len(ks) - 1) + "---|---|---|",
    ]
    for r in cases:
        seen = ", ".join(sorted(set(str(d) for d in r["decisions"])))
        pk = " | ".join(f"{r['pass_hat_k'][k]:.2f}" for k in ks if k != "1")
        flag = "**YES**" if r["unsafe_sends"] else ""
        out.append(
            f"| {r['id']} | {r['sample']} | {r['expect_decision']} | {r['pass_at_1']:.2f} | {pk} | "
            f"{seen} | {flag} | ${r['mean_cost_usd']:.4f} |"
        )
    out += ["", "## Failures", ""]
    any_fail = False
    for r in cases:
        for i, rr in enumerate(r["runs"]):
            if not rr["passed"]:
                any_fail = True
                bad = [k for k, v in rr["checks"].items() if not v]
                out.append(
                    f"- **{r['id']}** trial {i + 1}: failed {bad}; got decision={rr['decision']}, "
                    f"category={rr['category']}, actions={rr['actions']}, gate={rr['gate_reasons']}"
                    + (f", error={rr['error']}" if rr["error"] else "")
                )
    if not any_fail:
        out.append("None.")
    return "\n".join(out) + "\n"


def main(dataset: str, report: str, fail_under: float, trials: int = 1) -> int:
    report_path = Path(report)
    result = run(Path(dataset), trials, report_path.parent / ".eval_sent")
    report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    md_path = report_path.with_suffix(".md")
    md_path.write_text(render_markdown(result), encoding="utf-8")

    s = result["summary"]
    print(f"{'id':<4}{'expected':<16}{'pass@1':<8}{'pass^k':<8}{'decisions seen':<34}unsafe  cost")
    for r in result["cases"]:
        seen = ",".join(sorted(set(str(d) for d in r["decisions"])))
        pk = r["pass_hat_k"][str(trials)]
        print(
            f"{r['id']:<4}{r['expect_decision']:<16}{r['pass_at_1']:<8.2f}{pk:<8.2f}{seen:<34}"
            f"{r['unsafe_sends']:<8}${r['mean_cost_usd']:.4f}"
        )
    print("\nsummary:", json.dumps(s))
    print(f"report: {report_path}  {md_path}")
    ok = s["pass_at_1"] >= fail_under and s["unsafe_sends"] == 0 and s["failed_tickets"] == 0
    print("RESULT:", "PASS" if ok else "FAIL", f"(fail_under={fail_under}, trials={trials})")
    return 0 if ok else 1


if __name__ == "__main__":
    ds = sys.argv[1] if len(sys.argv) > 1 else str(PROJECT_ROOT / "evals" / "dataset.jsonl")
    k = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    raise SystemExit(main(ds, str(PROJECT_ROOT / "evals" / "report.json"), 0.9, k))
