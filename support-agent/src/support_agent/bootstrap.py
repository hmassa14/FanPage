"""Wire the application graph from settings. One place to look for what depends on what."""

from __future__ import annotations

import atexit

from .config import Settings, get_settings
from .delivery.sender import build_sender
from .knowledge.crm import CRM
from .knowledge.kb import KnowledgeBase
from .llm.factory import build_provider
from .logging_setup import configure_logging
from .pipeline.gates import Policy
from .pipeline.orchestrator import Pipeline
from .store.db import Store
from .tracing import configure_tracing

_KB_CACHE: dict[tuple[str, ...], KnowledgeBase] = {}


def build_knowledge_base(s: Settings) -> KnowledgeBase:
    """One knowledge base per process and configuration.

    It is read-only, and for hybrid retrieval it owns an embedded Weaviate process that
    must not be restarted per pipeline (the eval harness builds a pipeline per trial).
    """
    if s.retriever != "hybrid":
        return KnowledgeBase(s.kb_dir)
    key = (
        str(s.kb_dir),
        s.embedder,
        s.voyage_model,
        s.weaviate_mode,
        str(s.weaviate_data_dir),
        s.weaviate_url,
        str(s.weaviate_embedded_port),
        str(s.hybrid_alpha),
    )
    if key not in _KB_CACHE:
        from .knowledge.embeddings import build_embedder
        from .knowledge.vectorstore import WeaviateIndex

        _KB_CACHE[key] = KnowledgeBase(
            s.kb_dir,
            retriever="hybrid",
            index=WeaviateIndex(s),
            embedder=build_embedder(s),
            alpha=s.hybrid_alpha,
        )
    return _KB_CACHE[key]


def shutdown_knowledge_bases() -> None:
    for kb in _KB_CACHE.values():
        kb.close()
    _KB_CACHE.clear()


atexit.register(shutdown_knowledge_bases)


def build_pipeline(
    settings: Settings | None = None, *, store: Store | None = None, sender=None, provider=None
) -> Pipeline:
    s = settings or get_settings()
    configure_logging(s.log_level, s.log_json)
    configure_tracing(s)
    return Pipeline(
        settings=s,
        store=store or Store(s.db_path),
        provider=provider or build_provider(s),
        kb=build_knowledge_base(s),
        crm=CRM(s.crm_dir),
        sender=sender or build_sender(s),
        policy=Policy.load(s.policy_file),
    )
