from pathlib import Path

import pytest

from support_agent.models import ActionType, GateDecision, TicketStatus

from .conftest import load_sample


def test_return_within_window_auto_sends_with_capped_refund(pipeline, sent_dir: Path):
    t = pipeline.process_email(load_sample("01"))
    assert t.gate.decision == GateDecision.auto_send
    assert t.status == TicketStatus.sent
    assert [a.type for a in t.final_reply.approved_actions] == [ActionType.issue_refund]
    assert t.final_reply.approved_actions[0].amount_usd == 42.0
    assert len(list(sent_dir.glob("*.eml"))) == 1
    assert "get_order" in t.research.tools_used
    stages = [e["stage"] for e in pipeline.store.events(t.id)]
    assert stages[:4] == ["ingest", "redact", "triage", "research"] and "send" in stages


def test_damaged_expensive_item_needs_approval_then_human_approves(pipeline, sent_dir: Path):
    t = pipeline.process_email(load_sample("02"))
    assert t.status == TicketStatus.awaiting_approval
    assert any(r.rule == "actions.amount_cap" for r in t.gate.reasons)
    t2 = pipeline.approve(t.id, "haley", body=t.draft.body + "\nP.S. sorry again", note="ok")
    assert t2.status == TicketStatus.sent and t2.final_reply.edited is True
    with pytest.raises(ValueError):
        pipeline.approve(t.id, "haley")  # cannot approve twice


def test_chargeback_threat_escalates_and_never_sends(pipeline, sent_dir: Path):
    t = pipeline.process_email(load_sample("05"))
    assert t.status == TicketStatus.escalated and t.final_reply is None
    assert any(r.rule == "escalate.keyword" for r in t.gate.reasons)
    assert not sent_dir.exists() or not list(sent_dir.glob("*.eml"))


def test_spam_is_rejected_without_research(pipeline):
    t = pipeline.process_email(load_sample("06"))
    assert t.status == TicketStatus.rejected and t.research is None and t.draft is None


def test_card_number_is_redacted_before_model_and_never_persisted(pipeline):
    t = pipeline.process_email(load_sample("09"))
    stored = pipeline.store.get(t.id)
    assert "4111" not in stored.redacted.body_text
    assert "credit_card" in stored.redacted.redactions.values()
    assert "4111" in stored.email.body_text  # raw email is kept; redacted view is what the model saw
    events = pipeline.store.events(t.id)
    assert all("4111 1111" not in str(e["data"]) for e in events if e["stage"] != "ingest")


def test_duplicate_delivery_is_ignored(pipeline):
    a = pipeline.process_email(load_sample("03"))
    b = pipeline.process_email(load_sample("03"))
    assert a.id == b.id and pipeline.store.counts_by_status().get("sent") == 1


def test_german_cancellation_needs_human(pipeline):
    t = pipeline.process_email(load_sample("04"))
    assert t.status == TicketStatus.awaiting_approval
    rules = {r.rule for r in t.gate.reasons}
    assert "content.language" in rules and "actions.always_require_approval" in rules


def test_stage_failure_parks_ticket_as_failed(pipeline, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(pipeline.provider, "draft", boom)
    t = pipeline.process_email(load_sample("03"))
    assert t.status == TicketStatus.failed and "model down" in t.error


def test_judge_failure_fails_closed(pipeline, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("judge down")

    monkeypatch.setattr(pipeline.provider, "judge", boom)
    t = pipeline.process_email(load_sample("01"))
    assert t.status == TicketStatus.awaiting_approval
    assert any(r.rule == "judge.missing" for r in t.gate.reasons)


def test_prompt_injection_cannot_reach_the_refund_tool(pipeline, sent_dir: Path):
    t = pipeline.process_email(load_sample("14"))
    assert t.status == TicketStatus.awaiting_approval
    rules = {r.rule for r in t.gate.reasons}
    assert "content.injection_attempt" in rules
    # whatever the draft proposed, nothing executed and nothing was sent
    assert t.final_reply is None
    assert not any(e["stage"] == "action" for e in pipeline.store.events(t.id))
    assert not sent_dir.exists() or not list(sent_dir.glob("*.eml"))
    assert all(a.amount_usd is None or a.amount_usd <= 42.0 for a in t.draft.proposed_actions)


def test_final_sale_refusal_waits_for_a_human(pipeline):
    t = pipeline.process_email(load_sample("12"))
    assert t.status == TicketStatus.awaiting_approval
    assert any(r.rule == "auto_send.decline_review" for r in t.gate.reasons)


def test_side_effects_only_run_from_release(pipeline):
    """The only code path that executes a ProposedAction is Pipeline._release."""
    import inspect

    from support_agent.pipeline import orchestrator

    src = inspect.getsource(orchestrator)
    assert src.count('"executed ') == 1
    release_src = inspect.getsource(orchestrator.Pipeline._release)
    assert '"action"' in release_src and '"executed ' in release_src
