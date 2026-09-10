"""Domain models. Every pipeline stage has a typed input and a typed output.

These are the contracts between stages; they are also the JSON schemas used for
Claude structured outputs, so keep field descriptions precise: the model reads them.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Inbound
# --------------------------------------------------------------------------- #


class InboundEmail(BaseModel):
    message_id: str = Field(description="RFC 5322 Message-ID, or a synthetic one.")
    from_address: str
    from_name: str | None = None
    to_address: str
    subject: str
    body_text: str
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    thread_id: str | None = None
    attachments: list[str] = Field(default_factory=list, description="Attachment filenames only.")

    def idempotency_key(self) -> str:
        """Stable ticket id. Prefer the Message-ID; fall back to a content hash."""
        raw = self.message_id.strip() or f"{self.from_address}|{self.subject}|{self.body_text}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class RedactedEmail(BaseModel):
    """What the model is allowed to see. PII is replaced with placeholder tokens."""

    ticket_id: str
    from_address: str = Field(description="Customer email. Needed for CRM lookups.")
    from_name: str | None
    subject: str
    body_text: str
    received_at: datetime
    redactions: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "placeholder -> kind (e.g. '[[CARD_1]]' -> 'credit_card'). Originals are never stored here."
        ),
    )


# --------------------------------------------------------------------------- #
# Triage
# --------------------------------------------------------------------------- #


class TriageCategory(StrEnum):
    order_status = "order_status"
    shipping_issue = "shipping_issue"
    return_or_refund = "return_or_refund"
    product_question = "product_question"
    account_access = "account_access"
    billing = "billing"
    complaint = "complaint"
    legal_or_regulatory = "legal_or_regulatory"
    data_privacy_request = "data_privacy_request"
    spam_or_irrelevant = "spam_or_irrelevant"
    other = "other"


class Urgency(StrEnum):
    low = "low"
    normal = "normal"
    high = "high"
    critical = "critical"


class Sentiment(StrEnum):
    positive = "positive"
    neutral = "neutral"
    frustrated = "frustrated"
    angry = "angry"


class ExtractedEntities(BaseModel):
    order_ids: list[str] = Field(default_factory=list, description="Order numbers like NW-10042.")
    product_names: list[str] = Field(default_factory=list)
    amounts_usd: list[float] = Field(default_factory=list, description="Dollar amounts mentioned.")
    dates_mentioned: list[str] = Field(default_factory=list)


class TriageResult(BaseModel):
    category: TriageCategory
    secondary_categories: list[TriageCategory] = Field(default_factory=list)
    urgency: Urgency
    sentiment: Sentiment
    language: str = Field(description="ISO 639-1 code of the customer's email, e.g. 'en'.")
    summary: str = Field(description="One or two sentences. What happened and what they want.")
    customer_ask: str = Field(description="The concrete thing the customer is asking for.")
    entities: ExtractedEntities
    requires_human_reason: str | None = Field(
        default=None,
        description="If a human must handle this regardless of policy, say why. Otherwise null.",
    )
    confidence: float = Field(description="0.0-1.0 confidence in the primary category.")


# --------------------------------------------------------------------------- #
# Research
# --------------------------------------------------------------------------- #


class KBCitation(BaseModel):
    doc_id: str = Field(description="Knowledge-base document id, e.g. 'returns-policy'.")
    section: str = Field(description="Section heading the quote comes from.")
    quote: str = Field(description="Verbatim excerpt (<= 60 words) that supports the finding.")


class ResearchFinding(BaseModel):
    claim: str = Field(description="A fact the reply may rely on.")
    source: str = Field(description="Tool or doc that established it, e.g. 'get_order' or 'returns-policy'.")
    supporting_text: str = Field(description="The evidence, quoted or paraphrased closely.")


class ResearchBrief(BaseModel):
    customer_context: str = Field(description="Who the customer is per CRM (tier, tenure, history).")
    order_context: str = Field(description="Relevant order facts, or 'no order found'.")
    order_total_usd: float | None = Field(
        default=None, description="Total of the order this email is about, from get_order. Null if none."
    )
    relevant_policies: list[KBCitation]
    findings: list[ResearchFinding]
    open_questions: list[str] = Field(
        default_factory=list, description="Things we could not establish from tools or KB."
    )
    recommended_resolution: str = Field(description="What we should do for the customer and why.")
    tools_used: list[str] = Field(default_factory=list, description="Filled by the harness.")


# --------------------------------------------------------------------------- #
# Draft
# --------------------------------------------------------------------------- #


class ActionType(StrEnum):
    none = "none"
    issue_refund = "issue_refund"
    replace_item = "replace_item"
    cancel_order = "cancel_order"
    update_shipping_address = "update_shipping_address"
    reset_password_link = "reset_password_link"
    escalate_to_human = "escalate_to_human"


class ProposedAction(BaseModel):
    type: ActionType
    order_id: str | None = None
    amount_usd: float | None = Field(default=None, description="For refunds.")
    rationale: str = Field(description="Which policy or finding justifies this.")


class DraftReply(BaseModel):
    subject: str
    body: str = Field(description="Plain-text reply. Greeting, answer, next steps, sign-off.")
    proposed_actions: list[ProposedAction] = Field(
        description="Side effects the reply promises. Empty list if none. Executed only after gates pass."
    )
    cited_doc_ids: list[str] = Field(description="KB doc ids whose content the reply relies on.")
    declines_request: bool = Field(
        description="True if the reply refuses or only partially grants what the customer asked for."
    )
    confidence: float = Field(description="0.0-1.0 that this reply fully and correctly resolves the ask.")
    notes_for_reviewer: str = Field(description="Anything a human approver should double-check.")


# --------------------------------------------------------------------------- #
# Judge
# --------------------------------------------------------------------------- #


class GroundingVerdict(BaseModel):
    grounded: bool = Field(description="True only if every factual claim in the reply is supported.")
    score: float = Field(description="0.0-1.0 fraction of claims supported by the brief or KB.")
    unsupported_claims: list[str] = Field(default_factory=list)
    tone_ok: bool = Field(description="Professional, empathetic, no blame, no legal admissions.")
    policy_conflicts: list[str] = Field(
        default_factory=list, description="Places the reply contradicts a cited policy."
    )
    issues: list[str] = Field(default_factory=list, description="Anything else a reviewer should know.")


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #


class GateDecision(StrEnum):
    auto_send = "auto_send"
    needs_approval = "needs_approval"
    escalate = "escalate"
    reject = "reject"


Severity = Literal["info", "warn", "block", "escalate", "reject"]


class GateReason(BaseModel):
    rule: str
    severity: Severity
    detail: str


class GateResult(BaseModel):
    decision: GateDecision
    reasons: list[GateReason]
    policy_version: str


# --------------------------------------------------------------------------- #
# Ticket / bookkeeping
# --------------------------------------------------------------------------- #


class TicketStatus(StrEnum):
    received = "received"
    processing = "processing"
    awaiting_approval = "awaiting_approval"
    approved = "approved"
    sent = "sent"
    escalated = "escalated"
    rejected = "rejected"
    failed = "failed"


class StageUsage(BaseModel):
    stage: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0


class FinalReply(BaseModel):
    subject: str
    body: str
    approved_by: str | None = None
    edited: bool = False
    approved_actions: list[ProposedAction] = Field(default_factory=list)


class Ticket(BaseModel):
    id: str
    trace_id: str
    status: TicketStatus
    email: InboundEmail
    redacted: RedactedEmail | None = None
    triage: TriageResult | None = None
    research: ResearchBrief | None = None
    draft: DraftReply | None = None
    judge: GroundingVerdict | None = None
    gate: GateResult | None = None
    final_reply: FinalReply | None = None
    usage: list[StageUsage] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def total_cost_usd(self) -> float:
        return round(sum(u.cost_usd for u in self.usage), 6)
