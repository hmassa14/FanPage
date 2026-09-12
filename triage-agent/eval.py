"""Eval harness: run the agent over labelled cases, score it, keep the receipts.

    python eval.py                       # live API, 1 rep each
    python eval.py --reps 3 --workers 4  # pass^k needs reps
    python eval.py --client oracle       # offline: proves the harness scores a perfect run as 1.0
    python eval.py --client null         # offline: proves a constant answer scores badly
    python eval.py --rescore runs/<ts>/results.jsonl   # recompute metrics, no API calls

Every run writes to runs/<timestamp>/:
    results.jsonl  one row per attempt that produced a decision (with transcript)
    errors.jsonl   one row per attempt that did not (refusal, truncation, invalid JSON, API error)
    report.json    the numbers
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Any

import agent
import tracing
from agent import AgentError, triage
from fake_client import NullClient, OracleClient
from schemas import Ticket
from scoring import judge, render, score

HERE = Path(__file__).parent


def load_cases(path: Path) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    return cases


def _serialise(obj: Any) -> Any:
    """Transcripts hold SDK content blocks; turn them into plain JSON."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "__dict__") and not isinstance(obj, dict):
        return {k: _serialise(v) for k, v in vars(obj).items()}
    if isinstance(obj, dict):
        return {k: _serialise(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialise(v) for v in obj]
    return obj


def run_attempt(case: dict, rep: int, client: Any) -> dict:
    """Returns either {"ok": True, "row": ...} or {"ok": False, "error": ...}."""
    ticket = Ticket.model_validate(case["ticket"])
    try:
        # one parent span per attempt so every trace carries the case id
        with tracing.tracer.start_as_current_span(
            "eval.attempt", attributes={"triage.case_id": case["id"], "triage.rep": rep}
        ):
            result = triage(ticket, client=client)
    except AgentError as exc:
        return {"ok": False, "error": {"case_id": case["id"], "rep": rep, "kind": exc.kind, "detail": exc.detail}}
    except Exception as exc:  # anything else is a harness bug; still don't score it as wrong
        return {"ok": False, "error": {"case_id": case["id"], "rep": rep, "kind": "harness_error", "detail": repr(exc)}}

    tool_calls = [asdict(tc) for tc in result.tool_calls]
    predicted = result.decision.model_dump()
    row = {
        "case_id": case["id"],
        "rep": rep,
        "expected": case["expected"],
        "predicted": predicted,
        "tool_calls": tool_calls,
        **judge(case, predicted, tool_calls),
        "model": result.model,
        "turns": result.turns,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "latency_s": round(result.latency_s, 3),
        "transcript": _serialise(result.messages),
    }
    return {"ok": True, "row": row}


def run(cases: list[dict], reps: int, workers: int, client: Any) -> tuple[list[dict], list[dict]]:
    rows: list[dict] = []
    errors: list[dict] = []
    jobs = [(case, rep) for case in cases for rep in range(reps)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_attempt, case, rep, client): (case["id"], rep) for case, rep in jobs}
        for fut in as_completed(futures):
            outcome = fut.result()
            (rows if outcome["ok"] else errors).append(outcome.get("row") or outcome["error"])
            cid, rep = futures[fut]
            mark = "ok " if outcome["ok"] else "ERR"
            print(f"  [{mark}] {cid} rep{rep}", file=sys.stderr)
    rows.sort(key=lambda r: (r["case_id"], r["rep"]))
    errors.sort(key=lambda r: (r["case_id"], r["rep"]))
    return rows, errors


def make_client(kind: str, cases: list[dict]) -> Any:
    if kind == "api":
        import anthropic

        return anthropic.Anthropic()
    if kind == "oracle":
        return OracleClient(cases)
    if kind == "null":
        return NullClient()
    raise ValueError(kind)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", type=Path, default=HERE / "cases.jsonl")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--client", choices=["api", "oracle", "null"], default="api")
    ap.add_argument("--model", default=None, help="override TRIAGE_MODEL for this run")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--rescore", type=Path, default=None, help="score an existing results.jsonl instead of running")
    args = ap.parse_args(argv)

    cases = load_cases(args.cases)

    if args.rescore:
        rows = [json.loads(l) for l in args.rescore.read_text().splitlines() if l.strip()]
        err_path = args.rescore.with_name("errors.jsonl")
        n_err = sum(1 for l in err_path.read_text().splitlines() if l.strip()) if err_path.exists() else 0
        print(render(score(cases, rows, n_err)))
        return 0

    if args.model:
        agent.MODEL = args.model
    tracing.configure()  # TRIAGE_OTEL_EXPORTER=console|otlp; off by default
    client = make_client(args.client, cases)
    out = args.out or HERE / "runs" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)

    print(f"running {len(cases)} cases x {args.reps} reps with client={args.client} model={agent.MODEL}", file=sys.stderr)
    rows, errors = run(cases, args.reps, args.workers, client)

    (out / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (out / "errors.jsonl").write_text("".join(json.dumps(e) + "\n" for e in errors))
    report = score(cases, rows, len(errors))
    (out / "report.json").write_text(json.dumps(_serialise(report), indent=2, default=str))

    print(render(report))
    if errors:
        print("\nerrors (see errors.jsonl):")
        for e in errors:
            print(f"  {e['case_id']} rep{e['rep']}: {e['kind']} {e['detail'][:120]}")
    print(f"\nwrote {out}/", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
