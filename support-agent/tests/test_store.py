from support_agent.models import InboundEmail, StageUsage, TicketStatus
from support_agent.store.db import Store


def _email(mid="<a@b>"):
    return InboundEmail(message_id=mid, from_address="a@b.c", to_address="s@x.y", subject="s", body_text="b")


def test_idempotent_create_and_exclusive_claim():
    store = Store(":memory:")
    t1, created1 = store.create_ticket_if_new(_email(), "trace1")
    t2, created2 = store.create_ticket_if_new(_email(), "trace2")
    assert created1 and not created2 and t1.id == t2.id
    assert store.claim(t1.id) is True
    assert store.claim(t1.id) is False  # second worker loses


def test_usage_totals_and_events_roundtrip():
    store = Store(":memory:")
    t, _ = store.create_ticket_if_new(_email(), "tr")
    store.record_usage(
        t.id, StageUsage(stage="triage", model="m", input_tokens=10, output_tokens=5, cost_usd=0.001)
    )
    store.event(t.id, "triage", "done", foo="bar")
    assert store.usage_totals()["input_tokens"] == 10
    assert store.events(t.id)[0]["data"] == {"foo": "bar"}


def test_outbox_retry_then_fail():
    store = Store(":memory:")
    t, _ = store.create_ticket_if_new(_email(), "tr")
    store.enqueue_outbox(t.id, "a@b.c", "s", "b", None)
    store.enqueue_outbox(t.id, "a@b.c", "s", "b", None)  # idempotent per ticket
    rows = store.pending_outbox(max_attempts=2)
    assert len(rows) == 1
    store.mark_outbox(rows[0]["id"], sent=False, error="boom")
    store.mark_outbox(rows[0]["id"], sent=False, error="boom")
    assert store.pending_outbox(max_attempts=2) == []
    assert store.fail_exhausted_outbox(2) == 1
    assert store.outbox_for(t.id)["status"] == "failed"
    t.status = TicketStatus.failed
    store.save(t)
    assert store.counts_by_status() == {"failed": 1}
