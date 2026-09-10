"""Directory inbox: drop .eml or .json files into the inbox dir; processed files move aside.

This is the ingestion adapter for the demo and for integration tests. It is also a
perfectly reasonable production shape when an upstream system (a mail gateway, an
S3 event) delivers files.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..models import InboundEmail
from .parse import parse_eml, parse_json

logger = logging.getLogger(__name__)


class FileInboxSource:
    def __init__(self, inbox_dir: Path) -> None:
        self.inbox_dir = inbox_dir
        self.processed_dir = inbox_dir / "processed"
        self.failed_dir = inbox_dir / "failed"
        for d in (self.inbox_dir, self.processed_dir, self.failed_dir):
            d.mkdir(parents=True, exist_ok=True)

    def poll(self) -> list[tuple[InboundEmail, Path]]:
        found: list[tuple[InboundEmail, Path]] = []
        for path in sorted(p for p in self.inbox_dir.iterdir() if p.is_file()):
            try:
                if path.suffix.lower() == ".eml":
                    found.append((parse_eml(path.read_bytes()), path))
                elif path.suffix.lower() == ".json":
                    found.append((parse_json(json.loads(path.read_text(encoding="utf-8"))), path))
            except Exception as exc:  # noqa: BLE001 - quarantine unparseable input
                logger.error("could not parse %s: %s", path.name, exc)
                path.rename(self.failed_dir / path.name)
        return found

    def ack(self, path: Path) -> None:
        path.rename(self.processed_dir / path.name)
