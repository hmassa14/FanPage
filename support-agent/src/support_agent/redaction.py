"""PII redaction applied before any text reaches the model.

Deterministic regex redaction is the right first layer: it is auditable, cheap, and it
never hallucinates. The placeholders are stable per-ticket so the model can refer to
"[[CARD_1]]" consistently, and a gate later checks that no placeholder leaks into a reply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import InboundEmail, RedactedEmail

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 13-19 digit card numbers with optional spaces/dashes. Checked with Luhn below.
    ("credit_card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("phone", re.compile(r"(?<!\w)(?:\+?1[ -.]?)?\(?\d{3}\)?[ -.]?\d{3}[ -.]?\d{4}(?!\w)")),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
    ("password", re.compile(r"(?i)\b(?:password|passwd|pwd)\s*(?:is|:|=)\s*\S+")),
]

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass
class RedactionResult:
    text: str
    placeholders: dict[str, str] = field(default_factory=dict)  # placeholder -> kind
    originals: dict[str, str] = field(default_factory=dict)  # placeholder -> original (never persisted)


def redact_text(text: str, *, keep_email: str | None = None) -> RedactionResult:
    counters: dict[str, int] = {}
    result = RedactionResult(text=text)

    def _placeholder(kind: str) -> str:
        counters[kind] = counters.get(kind, 0) + 1
        return f"[[{kind.upper()}_{counters[kind]}]]"

    out = text
    for kind, pattern in _PATTERNS:

        def _sub(m: re.Match[str], kind: str = kind) -> str:
            raw = m.group(0)
            if kind == "credit_card":
                digits = re.sub(r"\D", "", raw)
                if not _luhn_ok(digits):
                    return raw  # probably an order number / tracking id, not a card
            ph = _placeholder(kind)
            result.placeholders[ph] = kind
            result.originals[ph] = raw
            return ph

        out = pattern.sub(_sub, out)

    # Third-party email addresses get redacted; the sender's own address is kept because
    # the CRM lookup needs it and the customer already knows it.
    def _sub_email(m: re.Match[str]) -> str:
        raw = m.group(0)
        if keep_email and raw.lower() == keep_email.lower():
            return raw
        ph = _placeholder("email")
        result.placeholders[ph] = "email"
        result.originals[ph] = raw
        return ph

    out = _EMAIL.sub(_sub_email, out)
    result.text = out
    return result


def redact_email(email: InboundEmail, ticket_id: str) -> tuple[RedactedEmail, dict[str, str]]:
    """Returns the model-safe email and the placeholder->original map (keep it out of the DB)."""
    body = redact_text(email.body_text, keep_email=email.from_address)
    subject = redact_text(email.subject, keep_email=email.from_address)
    placeholders = {**body.placeholders, **subject.placeholders}
    originals = {**body.originals, **subject.originals}
    redacted = RedactedEmail(
        ticket_id=ticket_id,
        from_address=email.from_address,
        from_name=email.from_name,
        subject=subject.text,
        body_text=body.text,
        received_at=email.received_at,
        redactions=placeholders,
    )
    return redacted, originals


PLACEHOLDER_RE = re.compile(r"\[\[[A-Z_]+_\d+\]\]")


def contains_placeholder(text: str) -> bool:
    return bool(PLACEHOLDER_RE.search(text))
