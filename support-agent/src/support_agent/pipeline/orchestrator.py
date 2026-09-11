"""The orchestrator: runs the stages in order and applies the gate decision.

Everything here is boring on purpose. Each stage is wrapped so that timing, token usage,
cost, and an audit event are recorded whether it succeeds or fails, and the ticket is
saved after every stage so a crash mid-pipeline leaves an inspectable record.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel

from ..config import Settings
from ..delivery.sender import EmailSender
from ..knowledge.crm import CRM
from ..knowledge.kb import KnowledgeBase
from ..knowledge.tools import ResearchTools
from ..llm.base import LLMProvider, StageOutput
from ..logging_setup import log, ticket_id_var, trace_id_var
from ..models import (
    ActionType,
    FinalReply,
    GateDecision,
    InboundEmail,
    Ticket,
    TicketStatus,
    TriageCategory,
)
from ..redaction import redact_email
from ..store.db import Store
from ..tracing import tracer
from . import gates

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        provider: LLMProvider,
        kb: KnowledgeBase,
        crm: CRM,
        sender: EmailSender,
        policy: gates.Policy,
    ) -> None:
        self.s, self.store, self.provider = settings, store, provider
        self.kb, self.crm, self.sender, self.policy = kb, crm, sender, policy

    # ---- entry point -------------------------------------------------------- #
    def process_email(self, email: InboundEmail) -> Ticket:
        trace_id = uuid.uuid4().hex
        ticket, created = self.store.create_ticket_if_new(email, trace_id)
        tok_t, tok_k = trace_id_var.set(ticket.trace_id), ticket_id_var.set(ticket.id)
        try:
            if not created:
                log(logger, logging.INFO, "duplicate email ignored", status=ticket.status.value)
                return ticket
            if not self.store.claim(ticket.id):
                log(logger, logging.INFO, "ticket already claimed by another worker")
                return ticket
            ticket.status = TicketStatus.processing
            self.store.event(
                ticket.id,
                "ingest",
                "ticket created",
                from_address=email.from_address,
                subject=email.subject,
            )
            with tracer().start_as_current_span("ticket.process") as span:
                span.set_attribute("ticket.id", ticket.id)
                span.set_attribute("ticket.trace_id", ticket.trace_id)
                span.set_attribute("email.subject", email.subject)
                try:
                    self._run(ticket)
                except Exception as exc:  # noqa: BLE001 - any stage failure parks the ticket
                    ticket.status, ticket.error = TicketStatus.failed, f"{type(exc).__name__}: {exc}"
                    self.store.event(ticket.id, "pipeline", "failed", level="error", error=ticket.error)
                    span.record_exception(exc)
                    logger.exception("pipeline failed")
                span.set_attribute("ticket.status", ticket.status.value)
                if ticket.gate:
                    span.set_attribute("gate.decision", ticket.gate.decision.value)
                span.set_attribute("ticket.cost_usd", ticket.total_cost_usd)
            self.store.save(ticket)
            return ticket
        finally:
            trace_id_var.reset(tok_t)
            ticket_id_var.reset(tok_k)

    # ---- stages ---------------------------------------------------------------- #
    def _stage(self, ticket: Ticket, name: str, fn: Callable[[], StageOutput[T]]) -> T:
        with tracer().start_as_current_span(f"stage.{name}") as span:
            out = fn()
            u = out.usage
            span.set_attributes(
                {
                    "llm.model": u.model,
                    "llm.input_tokens": u.input_tokens,
                    "llm.output_tokens": u.output_tokens,
                    "llm.cache_read_tokens": u.cache_read_tokens,
                    "llm.cost_usd": u.cost_usd,
                    "stage.latency_ms": u.latency_ms,
                }
            )
        ticket.usage.append(out.usage)
        self.store.record_usage(ticket.id, out.usage)
        self.store.event(
            ticket.id,
            name,
            "completed",
            model=out.usage.model,
            latency_ms=out.usage.latency_ms,
            cost_usd=out.usage.cost_usd,
            result=out.result.model_dump(mode="json"),
        )
        log(
            logger,
            logging.INFO,
            f"{name} done",
            latency_ms=out.usage.latency_ms,
            cost_usd=out.usage.cost_usd,
        )
        return out.result

    def _run(self, ticket: Ticket) -> None:
        redacted, _originals = redact_email(ticket.email, ticket.id)  # originals never persisted
        ticket.redacted = redacted
        self.store.event(ticket.id, "redact", "pii redacted", placeholders=redacted.redactions)
        self.store.save(ticket)

        ticket.triage = self._stage(ticket, "triage", lambda: self.provider.triage(redacted))
        self.store.save(ticket)
        triage = ticket.triage

        if triage.category == TriageCategory.spam_or_irrelevant:
            ticket.gate = gates.evaluate(self.policy, redacted, triage, None, None, None)
            self._apply(ticket)
            return

        tools = ResearchTools(kb=self.kb, crm=self.crm, customer_email=redacted.from_address)
        ticket.research = self._stage(
            ticket, "research", lambda: self.provider.research(redacted, triage, tools)
        )
        self.store.event(
            ticket.id,
            "research",
            "tool calls",
            calls=[{"name": c.name, "input": c.input, "ok": c.ok} for c in tools.calls],
        )
        self.store.save(ticket)

        brief = ticket.research
        ticket.draft = self._stage(ticket, "draft", lambda: self.provider.draft(redacted, triage, brief))
        self.store.save(ticket)

        draft = ticket.draft
        try:
            ticket.judge = self._stage(
                ticket,
                "judge",
                lambda: self.provider.judge(redacted, brief, draft),
            )
        except Exception as exc:  # noqa: BLE001 - judge failure is not fatal; gates fail closed
            ticket.judge = None
            self.store.event(
                ticket.id,
                "judge",
                "failed; gates will require approval",
                level="warning",
                error=str(exc),
            )
        with tracer().start_as_current_span("gate.evaluate") as span:
            ticket.gate = gates.evaluate(
                self.policy, redacted, triage, ticket.research, ticket.draft, ticket.judge
            )
            span.set_attribute("gate.decision", ticket.gate.decision.value)
            span.set_attribute("gate.policy_version", ticket.gate.policy_version)
            span.set_attribute("gate.reasons", [r.rule for r in ticket.gate.reasons])
        self._apply(ticket)

    # ---- decisions ------------------------------------------------------------- #
    def _apply(self, ticket: Ticket) -> None:
        assert ticket.gate is not None
        gate = ticket.gate
        self.store.event(
            ticket.id,
            "gate",
            gate.decision.value,
            policy_version=gate.policy_version,
            reasons=[r.model_dump() for r in gate.reasons],
        )
        log(
            logger,
            logging.INFO,
            "gate decision",
            decision=gate.decision.value,
            reasons=[f"{r.rule}: {r.detail}" for r in gate.reasons],
        )
        if gate.decision == GateDecision.auto_send:
            assert ticket.draft is not None
            ticket.final_reply = FinalReply(
                subject=ticket.draft.subject,
                body=ticket.draft.body,
                approved_by="policy:auto",
                approved_actions=list(ticket.draft.proposed_actions),
            )
            self._release(ticket)
        elif gate.decision == GateDecision.needs_approval:
            ticket.status = TicketStatus.awaiting_approval
        elif gate.decision == GateDecision.escalate:
            ticket.status = TicketStatus.escalated
        else:
            ticket.status = TicketStatus.rejected
        self.store.save(ticket)

    def _release(self, ticket: Ticket) -> None:
        """Approved reply: execute side effects, enqueue the email, try to send now."""
        assert ticket.final_reply is not None
        for action in ticket.final_reply.approved_actions:
            if action.type == ActionType.none:
                continue
            # A real deployment calls the order-management system here. It is the ONLY
            # place in the codebase that mutates customer state, and it runs only after gates.
            self.store.event(
                ticket.id,
                "action",
                f"executed {action.type.value} (mock)",
                order_id=action.order_id,
                amount_usd=action.amount_usd,
            )
        self.store.enqueue_outbox(
            ticket.id,
            ticket.email.from_address,
            ticket.final_reply.subject,
            ticket.final_reply.body,
            ticket.email.message_id,
        )
        ticket.status = TicketStatus.approved
        self.store.save(ticket)
        self.flush_outbox()
        fresh = self.store.get(ticket.id)  # flush may have advanced the status to `sent`
        if fresh:
            ticket.status = fresh.status

    # ---- human decisions --------------------------------------------------------- #
    def approve(
        self,
        ticket_id: str,
        reviewer: str,
        body: str | None = None,
        subject: str | None = None,
        note: str | None = None,
    ) -> Ticket:
        t = self._require(ticket_id, TicketStatus.awaiting_approval)
        assert t.draft is not None
        edited = bool(
            (body and body.strip() != t.draft.body.strip())
            or (subject and subject.strip() != t.draft.subject.strip())
        )
        t.final_reply = FinalReply(
            subject=(subject or t.draft.subject).strip(),
            body=(body or t.draft.body).strip(),
            approved_by=reviewer,
            edited=edited,
            approved_actions=list(t.draft.proposed_actions),
        )
        self.store.record_approval(t.id, reviewer, "approve", edited, note)
        self.store.event(t.id, "approval", "approved", reviewer=reviewer, edited=edited, note=note)
        self._release(t)
        return t

    def reject(self, ticket_id: str, reviewer: str, note: str | None = None) -> Ticket:
        t = self._require(ticket_id, TicketStatus.awaiting_approval)
        t.status = TicketStatus.rejected
        self.store.record_approval(t.id, reviewer, "reject", False, note)
        self.store.event(t.id, "approval", "rejected", reviewer=reviewer, note=note)
        self.store.save(t)
        return t

    def escalate(self, ticket_id: str, reviewer: str, note: str | None = None) -> Ticket:
        t = self._require(ticket_id, TicketStatus.awaiting_approval)
        t.status = TicketStatus.escalated
        self.store.record_approval(t.id, reviewer, "escalate", False, note)
        self.store.event(t.id, "approval", "escalated", reviewer=reviewer, note=note)
        self.store.save(t)
        return t

    def _require(self, ticket_id: str, status: TicketStatus) -> Ticket:
        t = self.store.get(ticket_id)
        if t is None:
            raise KeyError(f"unknown ticket {ticket_id}")
        if t.status != status:
            raise ValueError(f"ticket {ticket_id} is {t.status.value}, expected {status.value}")
        return t

    # ---- outbox ----------------------------------------------------------------- #
    def flush_outbox(self) -> int:
        """Send pending replies with bounded retries. Safe to call from a worker loop."""
        sent = 0
        for row in self.store.pending_outbox(self.s.send_max_attempts):
            try:
                with tracer().start_as_current_span("outbox.send") as span:
                    span.set_attribute("ticket.id", row["ticket_id"])
                    span.set_attribute("outbox.attempt", row["attempts"] + 1)
                    self.sender.send(row["to_address"], row["subject"], row["body"], row["in_reply_to"])
            except Exception as exc:  # noqa: BLE001 - record and retry later
                self.store.mark_outbox(row["id"], sent=False, error=str(exc))
                self.store.event(
                    row["ticket_id"],
                    "send",
                    "attempt failed",
                    level="warning",
                    error=str(exc),
                    attempts=row["attempts"] + 1,
                )
                continue
            self.store.mark_outbox(row["id"], sent=True)
            t = self.store.get(row["ticket_id"])
            if t:
                t.status = TicketStatus.sent
                self.store.save(t)
            self.store.event(row["ticket_id"], "send", "sent", to=row["to_address"])
            sent += 1
        failed = self.store.fail_exhausted_outbox(self.s.send_max_attempts)
        if failed:
            log(logger, logging.ERROR, "outbox entries exhausted retries", count=failed)
        return sent
