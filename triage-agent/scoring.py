"""Pure scoring functions. No I/O, no API - so the math is unit-testable.

Input is a list of result rows (one per (case, rep) that produced a decision)
plus the labelled cases. Rows that errored never reach here; the harness
reports them separately so plumbing failures don't masquerade as wrong answers.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from math import comb

from schemas import CATEGORIES


@dataclass
class ClassScores:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or (p + r) == 0:
            return None
        return 2 * p * r / (p + r)


@dataclass
class Report:
    n_cases: int
    n_attempts: int
    n_errors: int
    accuracy: float
    majority_baseline: float
    priority_exact: float
    tool_pass_rate: float
    pass_rate: float
    per_class: dict[str, ClassScores]
    macro_f1: float
    pass_pow_k: dict[int, float]  # k -> pass^k
    pass_at_k: dict[int, float]  # k -> pass@k
    per_case_passes: dict[str, tuple[int, int]] = field(default_factory=dict)  # case -> (passes, reps)
    failures: list[dict] = field(default_factory=list)


def judge(case: dict, predicted: dict, tool_calls: list[dict]) -> dict:
    """Per-attempt verdicts. `predicted` is the validated decision as a dict."""
    called_ok = {tc["name"] for tc in tool_calls if not tc["is_error"]}
    unknown = [tc for tc in tool_calls if tc["is_error"] and tc["content"].startswith("Unknown tool")]
    tools_ok = set(case["required_tools"]) <= called_ok and not unknown
    category_ok = predicted["category"] == case["expected"]["category"]
    priority_ok = predicted["priority"] == case["expected"]["priority"]
    return {
        "category_ok": category_ok,
        "priority_ok": priority_ok,
        "tools_ok": tools_ok,
        "passed": category_ok and priority_ok and tools_ok,
    }


def pass_pow_k(passes: int, reps: int, k: int) -> float:
    """P(all k sampled attempts pass), sampling k of `reps` without replacement."""
    if k > reps:
        raise ValueError(f"k={k} exceeds reps={reps}")
    return comb(passes, k) / comb(reps, k)


def pass_at_k(passes: int, reps: int, k: int) -> float:
    """P(at least one of k sampled attempts passes)."""
    if k > reps:
        raise ValueError(f"k={k} exceeds reps={reps}")
    return 1.0 - comb(reps - passes, k) / comb(reps, k)


def score(cases: list[dict], rows: list[dict], n_errors: int = 0) -> Report:
    by_id = {c["id"]: c for c in cases}
    per_class = {c: ClassScores() for c in CATEGORIES}
    per_case: dict[str, list[bool]] = defaultdict(list)
    failures = []
    n_cat = n_pri = n_tools = n_pass = 0

    for row in rows:
        case = by_id[row["case_id"]]
        exp, pred = case["expected"]["category"], row["predicted"]["category"]
        if exp == pred:
            per_class[exp].tp += 1
        else:
            per_class[exp].fn += 1
            per_class[pred].fp += 1
        n_cat += row["category_ok"]
        n_pri += row["priority_ok"]
        n_tools += row["tools_ok"]
        n_pass += row["passed"]
        per_case[row["case_id"]].append(row["passed"])
        if not row["passed"]:
            failures.append(
                {
                    "case_id": row["case_id"],
                    "rep": row["rep"],
                    "expected": case["expected"],
                    "predicted": {"category": pred, "priority": row["predicted"]["priority"]},
                    "tools_ok": row["tools_ok"],
                    "tool_calls": [tc["name"] for tc in row["tool_calls"]],
                }
            )

    n = len(rows)
    majority = Counter(c["expected"]["category"] for c in cases).most_common(1)[0][1] / len(cases)
    f1s = [s.f1 for s in per_class.values() if s.f1 is not None or (s.tp + s.fn) > 0]
    macro_f1 = sum((f or 0.0) for f in f1s) / len(f1s) if f1s else 0.0

    reps = min((len(v) for v in per_case.values()), default=0)
    pow_k: dict[int, float] = {}
    at_k: dict[int, float] = {}
    for k in range(1, reps + 1):
        pow_k[k] = sum(pass_pow_k(sum(v), len(v), k) for v in per_case.values()) / len(per_case)
        at_k[k] = sum(pass_at_k(sum(v), len(v), k) for v in per_case.values()) / len(per_case)

    return Report(
        n_cases=len(cases),
        n_attempts=n,
        n_errors=n_errors,
        accuracy=n_cat / n if n else 0.0,
        majority_baseline=majority,
        priority_exact=n_pri / n if n else 0.0,
        tool_pass_rate=n_tools / n if n else 0.0,
        pass_rate=n_pass / n if n else 0.0,
        per_class=per_class,
        macro_f1=macro_f1,
        pass_pow_k=pow_k,
        pass_at_k=at_k,
        per_case_passes={cid: (sum(v), len(v)) for cid, v in per_case.items()},
        failures=failures,
    )


def _fmt(x: float | None) -> str:
    return "  -  " if x is None else f"{x:5.2f}"


def render(report: Report) -> str:
    lines = [
        f"cases={report.n_cases}  attempts={report.n_attempts}  errors(not scored)={report.n_errors}",
        "",
        f"category accuracy   {report.accuracy:.3f}   (majority-class baseline {report.majority_baseline:.3f})",
        f"macro F1            {report.macro_f1:.3f}",
        f"priority exact      {report.priority_exact:.3f}",
        f"tool pass-rate      {report.tool_pass_rate:.3f}   (required tools called, valid input, no unknown tools)",
        f"pass rate           {report.pass_rate:.3f}   (category AND priority AND tools)",
        "",
        f"{'class':<16}{'P':>7}{'R':>7}{'F1':>7}{'tp':>5}{'fp':>5}{'fn':>5}",
    ]
    for name, s in report.per_class.items():
        lines.append(f"{name:<16}{_fmt(s.precision):>7}{_fmt(s.recall):>7}{_fmt(s.f1):>7}{s.tp:>5}{s.fp:>5}{s.fn:>5}")
    if report.pass_pow_k:
        lines.append("")
        lines.append("k     pass^k   pass@k")
        for k in report.pass_pow_k:
            lines.append(f"{k:<6}{report.pass_pow_k[k]:.3f}    {report.pass_at_k[k]:.3f}")
    if report.failures:
        lines.append("")
        lines.append("failures:")
        for f in report.failures:
            lines.append(
                f"  {f['case_id']} rep{f['rep']}: expected {f['expected']['category']}/{f['expected']['priority']}, "
                f"got {f['predicted']['category']}/{f['predicted']['priority']}, tools_ok={f['tools_ok']} {f['tool_calls']}"
            )
    return "\n".join(lines)
