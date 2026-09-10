"""Outbound email adapters. The pipeline only knows the `EmailSender` protocol."""

from __future__ import annotations

import logging
import smtplib
import sys
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol

from ..config import Settings

logger = logging.getLogger(__name__)


class EmailSender(Protocol):
    def send(self, to_address: str, subject: str, body: str, in_reply_to: str | None) -> None: ...


def build_message(
    from_address: str, to_address: str, subject: str, body: str, in_reply_to: str | None
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = from_address, to_address, subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body, cte="8bit")
    return msg


class ConsoleSender:
    """Prints the email. Default for local demos."""

    def __init__(self, from_address: str, stream=sys.stdout) -> None:
        self.from_address, self.stream = from_address, stream

    def send(self, to_address: str, subject: str, body: str, in_reply_to: str | None) -> None:
        msg = build_message(self.from_address, to_address, subject, body, in_reply_to)
        self.stream.write("\n" + "=" * 72 + "\n" + msg.as_string() + "=" * 72 + "\n")
        self.stream.flush()


class FileSender:
    """Writes .eml files to a directory. Handy for inspecting output in CI."""

    def __init__(self, from_address: str, out_dir: Path) -> None:
        self.from_address, self.out_dir = from_address, out_dir
        out_dir.mkdir(parents=True, exist_ok=True)

    def send(self, to_address: str, subject: str, body: str, in_reply_to: str | None) -> None:
        msg = build_message(self.from_address, to_address, subject, body, in_reply_to)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        (self.out_dir / f"{stamp}.eml").write_text(msg.as_string(), encoding="utf-8")


class SMTPSender:
    def __init__(self, s: Settings) -> None:
        self.s = s

    def send(self, to_address: str, subject: str, body: str, in_reply_to: str | None) -> None:
        msg = build_message(self.s.support_address, to_address, subject, body, in_reply_to)
        with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=30) as smtp:
            if self.s.smtp_starttls:
                smtp.starttls()
            if self.s.smtp_username and self.s.smtp_password:
                smtp.login(self.s.smtp_username, self.s.smtp_password)
            smtp.send_message(msg)


def build_sender(s: Settings) -> EmailSender:
    if s.sender == "smtp":
        return SMTPSender(s)
    if s.sender == "file":
        return FileSender(s.support_address, s.outbox_dir)
    return ConsoleSender(s.support_address)
