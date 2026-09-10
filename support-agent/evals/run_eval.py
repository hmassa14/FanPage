"""Eval harness: replay a labeled email set through the full pipeline and score it.

What is scored (per case, then aggregated):
  triage_accuracy   primary category == expected (cases with expect_category)
  decision_accuracy gate decision == expected            <- the one that matters most
  action_accuracy   set of proposed action types == expected (cases with expect_actions)
  content_checks    must_include / must_not_include phrases against the reply body
  unsafe_sends      auto_send when a human was expected: counted separately, and any > 0 fails

Runs offline on the fake provider (CI) or against Claude (set SA_LLM_PROVIDER=anthropic).
Cost and latency per case are reported so a prompt change that doubles spend is visible.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from support_agent.bootstrap import build_pipeline
from support_agent.config import PROJECT_ROOT, Settings
from support_agent.delivery.sender import FileSender
from support_agent.ingest.parse import parse_json
from support_agent.models import GateDecision
from support_agent.store.db import Store

HUMAN_DECISIONS = {GateDecision.needs_approval.value, GateDecision.escalate.value}


def run_case(pipeline, case: dict) -> dict:
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
    res = {
        "id": case["id"],
        "sample": case["sample"],
        "status": t.status.value,
        "error": t.error,
        "category": category,
        "expect_category": case.get("expect_category"),
        "decision": decision,
        "expect_decision": case["expect_decision"],
        "actions": actions,
        "expect_actions": case.get("expect_actions"),
        "cost_usd": t.total_cost_usd,
        "latency_s": round(elapsed, 2),
        "checks": {},
    }
    if case.get("expect_category") is not None:
        res["checks"]["triage"] = category == case["expect_category"]
    res["checks"]["decision"] = decision == case["expect_decision"]
    if case.get("expect_actions") is not None:
        res["checks"]["actions"] = actions == sorted(case["expect_actions"])
    content_ok = all(p.lower() in body.lower() for p in case.get("must_include", [])) and not any(
        p.lower() in body.lower() for p in case.get("must_not_include", [])
    )
    if case.get("must_include") or case.get("must_not_include"):
        res["checks"]["content"] = content_ok
    res["unsafe_send"] = decision == "auto_send" and case["expect_decision"] in HUMAN_DECISIONS
    return res


def main(dataset: str, report: str, fail_under: float) -> int:
    settings = Settings()
    tmp = Path(report).parent / ".eval_sent"
    pipeline = build_pipeline(
        settings, store=Store(":memory:"), sender=FileSender(settings.support_address, tmp)
    )
    cases = [
        json.loads(line) for line in Path(dataset).read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    results = [run_case(pipeline, c) for c in cases]

    def rate(key: str) -> float | None:
        vals = [r["checks"][key] for r in results if key in r["checks"]]
        return round(sum(vals) / len(vals), 3) if vals else None

    summary = {
        "provider": pipeline.provider.name,
        "policy_version": pipeline.policy.version,
        "n": len(results),
        "triage_accuracy": rate("triage"),
        "decision_accuracy": rate("decision"),
        "action_accuracy": rate("actions"),
        "content_checks": rate("content"),
        "unsafe_sends": sum(r["unsafe_send"] for r in results),
        "failed_tickets": sum(1 for r in results if r["status"] == "failed"),
        "total_cost_usd": round(sum(r["cost_usd"] for r in results), 4),
        "mean_latency_s": round(sum(r["latency_s"] for r in results) / max(len(results), 1), 2),
    }
    Path(report).write_text(json.dumps({"summary": summary, "cases": results}, indent=2), encoding="utf-8")

    print(f"{'id':<4}{'category':<24}{'decision':<16}{'expected':<16}{'checks':<40}cost")
    for r in results:
        checks = " ".join(f"{k}={'ok' if v else 'FAIL'}" for k, v in r["checks"].items())
        flag = "  <-- UNSAFE SEND" if r["unsafe_send"] else ""
        print(
            f"{r['id']:<4}{str(r['category']):<24}{str(r['decision']):<16}{r['expect_decision']:<16}"
            f"{checks:<40}${r['cost_usd']:.4f}{flag}"
        )
    print("\nsummary:", json.dumps(summary))
    ok = (
        (summary["decision_accuracy"] or 0) >= fail_under
        and summary["unsafe_sends"] == 0
        and summary["failed_tickets"] == 0
    )
    print("RESULT:", "PASS" if ok else "FAIL", f"(fail_under={fail_under})")
    return 0 if ok else 1


if __name__ == "__main__":
    ds = sys.argv[1] if len(sys.argv) > 1 else str(PROJECT_ROOT / "evals" / "dataset.jsonl")
    raise SystemExit(main(ds, str(PROJECT_ROOT / "evals" / "report.json"), 0.9))
