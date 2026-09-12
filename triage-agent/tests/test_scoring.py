"""Scoring math, checked against runs with known planted failures."""

from pathlib import Path

import pytest

import eval as harness
from fake_client import NullClient, OracleClient, Plant
from scoring import pass_at_k, pass_pow_k, score

CASES = harness.load_cases(Path(__file__).parent.parent / "cases.jsonl")


def _run(client, reps=1):
    rows, errors = harness.run(CASES, reps=reps, workers=2, client=client)
    return score(CASES, rows, len(errors)), errors


def test_case_set_shape():
    assert len(CASES) == 12
    from collections import Counter

    counts = Counter(c["expected"]["category"] for c in CASES)
    assert counts == {"billing": 3, "bug": 3, "feature_request": 2, "account_access": 2, "churn_risk": 2}


def test_oracle_scores_perfect():
    report, errors = _run(OracleClient(CASES))
    assert errors == []
    assert report.accuracy == 1.0 and report.tool_pass_rate == 1.0 and report.pass_rate == 1.0
    assert all(s.f1 == 1.0 for s in report.per_class.values())
    assert report.pass_pow_k == {1: 1.0}


def test_null_baseline_scores_badly():
    report, _ = _run(NullClient("billing", "P2"))
    assert report.accuracy == pytest.approx(3 / 12) == report.majority_baseline
    assert report.tool_pass_rate == 0.0  # never called lookup_customer
    assert report.per_class["billing"].precision == pytest.approx(3 / 12)
    assert report.per_class["churn_risk"].recall == 0.0


def test_two_planted_failures():
    plant = {
        "c2": Plant(category="billing"),  # churn_risk decoy mislabelled as billing
        "g3": Plant(skip_tools=True),  # right answer, but never looked anything up
    }
    report, errors = _run(OracleClient(CASES, plant=plant))
    assert errors == []
    assert report.per_class["churn_risk"].recall == pytest.approx(0.5)
    assert report.per_class["billing"].precision == pytest.approx(0.75)
    assert report.per_class["billing"].recall == 1.0
    assert report.accuracy == pytest.approx(11 / 12)
    assert report.tool_pass_rate == pytest.approx(11 / 12)
    assert report.pass_rate == pytest.approx(10 / 12)
    assert {f["case_id"] for f in report.failures} == {"c2", "g3"}


def test_pass_pow_k_with_reps():
    # c2 fails every rep, g3 fails 1 of 3 (planted via a flaky client)
    class Flaky(OracleClient):
        def __init__(self, cases):
            super().__init__(cases, plant={"c2": Plant(category="billing")})
            self._seen = 0

        def _answer(self, messages, **kw):
            if messages[0]["content"].startswith("customer_id: cust_008") and len(messages) == 1:
                self._seen += 1
                if self._seen == 1:
                    self.plant["g3"] = Plant(skip_tools=True)
                else:
                    self.plant.pop("g3", None)
            return super()._answer(messages, **kw)

    report, _ = _run(Flaky(CASES), reps=3)
    assert report.per_case_passes["c2"] == (0, 3)
    assert report.per_case_passes["g3"] == (2, 3)
    # 10 cases always pass, c2 never, g3 with prob 2/3 (k=1) or 1/3 (k=2) or 0 (k=3)
    assert report.pass_pow_k[1] == pytest.approx((10 + 0 + 2 / 3) / 12)
    assert report.pass_pow_k[2] == pytest.approx((10 + 0 + 1 / 3) / 12)
    assert report.pass_pow_k[3] == pytest.approx(10 / 12)
    assert report.pass_at_k[3] == pytest.approx(11 / 12)


def test_estimators_edge_cases():
    assert pass_pow_k(3, 3, 3) == 1.0 and pass_pow_k(2, 3, 3) == 0.0
    assert pass_at_k(1, 3, 3) == 1.0 and pass_at_k(0, 3, 2) == 0.0
    assert pass_pow_k(2, 4, 2) == pytest.approx(1 / 6)
    with pytest.raises(ValueError):
        pass_pow_k(1, 1, 2)
