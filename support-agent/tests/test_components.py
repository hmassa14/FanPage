import sys
from pathlib import Path

from support_agent.config import PROJECT_ROOT

sys.path.insert(0, str(PROJECT_ROOT / "evals"))
from components import render_scorecard, run_components  # noqa: E402


def test_component_scorecard_runs_offline(settings, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SA_LLM_PROVIDER", "fake")
    monkeypatch.setenv("SA_LOG_LEVEL", "ERROR")
    sc = run_components(
        PROJECT_ROOT / "evals" / "dataset.jsonl",
        PROJECT_ROOT / "evals" / "retrieval.jsonl",
        tmp_path / "sent",
    )
    assert sc["triage"]["accuracy"] == 1.0
    assert sc["retrieval"]["bm25"]["recall_at_3"] >= 0.85 and sc["retrieval"]["bm25"]["mrr"] >= 0.75
    assert sc["draft"]["rubric_pass_rate"]["no_placeholder"] == 1.0
    # the offline judge must catch every seeded corruption and pass every clean draft
    assert sc["judge"]["detection_mean"] == 1.0
    assert sc["judge"]["false_positive_rate_on_clean"] == 0.0
    assert sc["gates"]["decision_agreement"] == 1.0
    md = render_scorecard(
        sc,
        {
            "pass_at_1": 1.0,
            "pass_hat_k": {"1": 1.0, "3": 1.0},
            "cases": 14,
            "trials_per_case": 3,
            "unsafe_sends": 0,
        },
    )
    assert "| retrieval (bm25) |" in md and "pass^3" in md
