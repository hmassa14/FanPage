"""Wire the application graph from settings. One place to look for what depends on what."""

from __future__ import annotations

from .config import Settings, get_settings
from .delivery.sender import build_sender
from .knowledge.crm import CRM
from .knowledge.kb import KnowledgeBase
from .llm.factory import build_provider
from .logging_setup import configure_logging
from .pipeline.gates import Policy
from .pipeline.orchestrator import Pipeline
from .store.db import Store


def build_pipeline(
    settings: Settings | None = None, *, store: Store | None = None, sender=None, provider=None
) -> Pipeline:
    s = settings or get_settings()
    configure_logging(s.log_level, s.log_json)
    return Pipeline(
        settings=s,
        store=store or Store(s.db_path),
        provider=provider or build_provider(s),
        kb=KnowledgeBase(s.kb_dir),
        crm=CRM(s.crm_dir),
        sender=sender or build_sender(s),
        policy=Policy.load(s.policy_file),
    )
