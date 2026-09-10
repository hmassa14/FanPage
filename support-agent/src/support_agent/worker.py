"""Polling worker: pulls from ingestion sources, runs the pipeline, flushes the outbox.

Run several of these against the same database: `Store.claim` makes ticket processing
exclusive, and the outbox is idempotent per ticket.
"""

from __future__ import annotations

import logging
import signal
import time
from typing import Any

from .ingest.file_source import FileInboxSource
from .logging_setup import log
from .pipeline.orchestrator import Pipeline

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, pipeline: Pipeline, sources: list[Any], poll_seconds: float) -> None:
        self.pipeline, self.sources, self.poll_seconds = pipeline, sources, poll_seconds
        self._stop = False

    def stop(self, *_: Any) -> None:
        self._stop = True

    def tick(self) -> int:
        processed = 0
        for source in self.sources:
            for email, handle in source.poll():
                self.pipeline.process_email(email)
                source.ack(handle)
                processed += 1
        self.pipeline.flush_outbox()
        return processed

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        log(
            logger,
            logging.INFO,
            "worker started",
            poll_seconds=self.poll_seconds,
            sources=[type(s).__name__ for s in self.sources],
        )
        while not self._stop:
            n = self.tick()
            if n == 0:
                time.sleep(self.poll_seconds)
        log(logger, logging.INFO, "worker stopped")


def default_sources(pipeline: Pipeline) -> list[Any]:
    s = pipeline.s
    sources: list[Any] = [FileInboxSource(s.inbox_dir)]
    if s.imap_host:
        from .ingest.imap_source import ImapSource

        sources.append(ImapSource(s))
    return sources
