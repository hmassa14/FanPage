"""IMAP inbox adapter (stdlib only). Marks messages as \\Seen after a successful handoff.

Idempotency lives in the store (ticket id = hash of Message-ID), so re-delivering a message
after a crash is harmless.
"""

from __future__ import annotations

import imaplib
import logging

from ..config import Settings
from ..models import InboundEmail
from .parse import parse_eml

logger = logging.getLogger(__name__)


class ImapSource:
    def __init__(self, s: Settings) -> None:
        if not (s.imap_host and s.imap_username and s.imap_password):
            raise ValueError("SA_IMAP_HOST, SA_IMAP_USERNAME and SA_IMAP_PASSWORD are required")
        self.s = s
        self.host: str = s.imap_host

    def poll(self) -> list[tuple[InboundEmail, bytes]]:
        out: list[tuple[InboundEmail, bytes]] = []
        with imaplib.IMAP4_SSL(self.host) as imap:
            imap.login(self.s.imap_username, self.s.imap_password)  # type: ignore[arg-type]
            imap.select(self.s.imap_folder)
            _, data = imap.search(None, "UNSEEN")
            for uid in data[0].split():
                _, msg_data = imap.fetch(uid, "(BODY.PEEK[])")
                raw = msg_data[0][1] if msg_data and isinstance(msg_data[0], tuple) else None
                if not raw:
                    continue
                try:
                    out.append((parse_eml(raw), uid))
                except Exception as exc:  # noqa: BLE001
                    logger.error("could not parse IMAP uid %s: %s", uid, exc)
        return out

    def ack(self, uid: bytes) -> None:
        with imaplib.IMAP4_SSL(self.host) as imap:
            imap.login(self.s.imap_username, self.s.imap_password)  # type: ignore[arg-type]
            imap.select(self.s.imap_folder)
            imap.store(uid.decode(), "+FLAGS", "\\Seen")
