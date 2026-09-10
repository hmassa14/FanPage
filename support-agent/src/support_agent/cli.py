"""Command-line entry points.

support-agent demo                 run the bundled sample emails end to end
support-agent process FILE...      process .eml/.json files
support-agent show TICKET_ID       print a ticket and its audit trail
support-agent serve                start the approval API/UI
support-agent worker               poll the inbox directory (and IMAP if configured)
support-agent eval                 run the eval harness (see evals/)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import PROJECT_ROOT, get_settings
from .ingest.parse import parse_eml, parse_json
from .models import Ticket


def _load(path: Path):
    if path.suffix.lower() == ".eml":
        return parse_eml(path.read_bytes())
    return parse_json(json.loads(path.read_text(encoding="utf-8")))


def _summary_line(t: Ticket) -> str:
    cat = t.triage.category.value if t.triage else "-"
    dec = t.gate.decision.value if t.gate else "-"
    actions = (
        ",".join(
            f"{a.type.value}{'$' + format(a.amount_usd, '.2f') if a.amount_usd else ''}"
            for a in (t.draft.proposed_actions if t.draft else [])
        )
        or "-"
    )
    return (
        f"{t.id[:10]}  {t.status.value:<18} {cat:<22} {dec:<15} {actions:<28} "
        f"${t.total_cost_usd:.4f}  {t.email.subject[:40]}"
    )


def cmd_process(args: argparse.Namespace) -> int:
    from .bootstrap import build_pipeline

    pipeline = build_pipeline()
    print(f"{'ticket':<11} {'status':<18} {'category':<22} {'gate':<15} {'actions':<28} cost     subject")
    for p in args.files:
        t = pipeline.process_email(_load(Path(p)))
        print(_summary_line(t))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    args.files = sorted(str(p) for p in (PROJECT_ROOT / "data" / "samples").glob("*.json"))
    rc = cmd_process(args)
    s = get_settings()
    print(f"\nProvider: {s.llm_provider}. Approval UI: support-agent serve  ->  http://127.0.0.1:8000/")
    return rc


def cmd_show(args: argparse.Namespace) -> int:
    from .store.db import Store

    store = Store(get_settings().db_path)
    t = store.get(args.ticket_id)
    if not t:
        print("no such ticket", file=sys.stderr)
        return 1
    print(t.model_dump_json(indent=2))
    print("\n--- audit trail ---")
    for e in store.events(t.id):
        print(f"{e['ts']}  {e['stage']:<10} {e['level']:<7} {e['message']}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api.app import create_app
    from .bootstrap import build_pipeline

    app = create_app(build_pipeline())
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    from .bootstrap import build_pipeline
    from .worker import Worker, default_sources

    pipeline = build_pipeline()
    Worker(pipeline, default_sources(pipeline), pipeline.s.worker_poll_seconds).run_forever()
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(PROJECT_ROOT / "evals"))
    from run_eval import main as eval_main  # type: ignore[import-not-found]

    return eval_main(args.dataset, args.report, args.fail_under)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="support-agent",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("process")
    p.add_argument("files", nargs="+")
    p.set_defaults(fn=cmd_process)
    p = sub.add_parser("demo")
    p.set_defaults(fn=cmd_demo)
    p = sub.add_parser("show")
    p.add_argument("ticket_id")
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("worker")
    p.set_defaults(fn=cmd_worker)
    p = sub.add_parser("eval")
    p.add_argument("--dataset", default=str(PROJECT_ROOT / "evals" / "dataset.jsonl"))
    p.add_argument("--report", default=str(PROJECT_ROOT / "evals" / "report.json"))
    p.add_argument("--fail-under", type=float, default=0.9)
    p.set_defaults(fn=cmd_eval)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
