"""Turn raw inputs (.eml bytes or a JSON dict) into an `InboundEmail`."""

from __future__ import annotations

import email
import email.policy
import email.utils
import uuid
from datetime import UTC, datetime
from typing import Any

from ..models import InboundEmail


def parse_eml(raw: bytes) -> InboundEmail:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    from_name, from_addr = email.utils.parseaddr(msg.get("From", ""))
    _, to_addr = email.utils.parseaddr(msg.get("To", ""))
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = body_part.get_content() if body_part else ""
    if body_part is not None and body_part.get_content_type() == "text/html":
        body = _strip_html(body)
    received = None
    if msg.get("Date"):
        try:
            received = email.utils.parsedate_to_datetime(msg["Date"])
            if received.tzinfo is None:
                received = received.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            received = None
    attachments = [fn for p in msg.iter_attachments() if (fn := p.get_filename())]
    return InboundEmail(
        message_id=msg.get("Message-ID") or f"<{uuid.uuid4()}@generated>",
        from_address=from_addr,
        from_name=from_name or None,
        to_address=to_addr,
        subject=msg.get("Subject", "(no subject)"),
        body_text=body.strip(),
        received_at=received or datetime.now(UTC),
        thread_id=msg.get("In-Reply-To") or None,
        attachments=attachments,
    )


def parse_json(data: dict[str, Any]) -> InboundEmail:
    data = dict(data)
    data.setdefault("message_id", f"<{uuid.uuid4()}@generated>")
    data.setdefault("to_address", "support@example.com")
    return InboundEmail.model_validate(data)


def _strip_html(html: str) -> str:
    import re

    text = re.sub(r"<(br|/p|/div)\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text)
