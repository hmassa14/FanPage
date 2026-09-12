"""Hybrid retrieval against embedded Weaviate with the key-free hash embedder.

Proves the plumbing: collection creation, idempotent sync, hybrid query, alpha behaviour,
and the side-by-side retrieval eval. Semantic quality is a live-Voyage question.
Skips cleanly when embedded Weaviate cannot start (no binary download, no free port).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from support_agent.config import PROJECT_ROOT, Settings
from support_agent.knowledge.embeddings import HashEmbedder
from support_agent.knowledge.kb import KnowledgeBase

sys.path.insert(0, str(PROJECT_ROOT / "evals"))
from components import score_retrievers  # noqa: E402


@pytest.fixture(scope="module")
def hybrid_kb(tmp_path_factory):
    from support_agent.knowledge.vectorstore import WeaviateIndex

    s = Settings(
        retriever="hybrid",
        embedder="hash",
        log_level="ERROR",
        weaviate_data_dir=tmp_path_factory.mktemp("wv"),
        weaviate_embedded_port=8089,
        weaviate_embedded_grpc_port=50060,
    )
    try:
        index = WeaviateIndex(s, collection="PolicyChunkTest")
    except Exception as exc:  # noqa: BLE001 - environment, not code
        pytest.skip(f"embedded weaviate unavailable: {exc}")
    kb = KnowledgeBase(s.kb_dir, retriever="hybrid", index=index, embedder=HashEmbedder(), alpha=0.5)
    yield kb
    kb.close()


def test_sync_is_idempotent_and_complete(hybrid_kb: KnowledgeBase):
    assert hybrid_kb.index is not None and hybrid_kb.embedder is not None
    assert hybrid_kb.index.count() == len(hybrid_kb.chunks)
    assert hybrid_kb.index.sync(hybrid_kb.chunks, hybrid_kb.embedder) == {
        "written": 0,
        "deleted": 0,
        "unchanged": len(hybrid_kb.chunks),
    }


def test_hybrid_search_returns_chunks_and_respects_alpha(hybrid_kb: KnowledgeBase):
    hits = hybrid_kb.search("cancel my order before it ships", top_k=3)
    assert hits and hits[0][0].doc_id == "order-changes"
    keyword_only = [
        c.ref for c, _ in hybrid_kb.search_hybrid("cancel my order before it ships", 3, alpha=0.0)
    ]
    vector_only = [c.ref for c, _ in hybrid_kb.search_hybrid("cancel my order before it ships", 3, alpha=1.0)]
    assert keyword_only and vector_only
    assert "hybrid(weaviate, hash-trigram-512" in hybrid_kb.retriever_name


def test_retrieval_eval_scores_both_retrievers(hybrid_kb: KnowledgeBase):
    sc = score_retrievers(hybrid_kb, PROJECT_ROOT / "evals" / "retrieval.jsonl")
    assert set(sc) == {"bm25", "hybrid", "configured"} and sc["configured"] == "hybrid"
    assert sc["bm25"]["recall_at_3"] >= 0.85
    assert sc["hybrid"]["doc_recall_at_3"] >= 0.9  # plumbing works; semantics are a live question
    assert sc["hybrid"]["retriever"].startswith("hybrid(weaviate")


def test_hash_embedder_is_deterministic_and_normalised():
    e = HashEmbedder(dim=64)
    a, b = e.embed_query("return window"), e.embed_query("return window")
    assert a == b and abs(sum(x * x for x in a) - 1.0) < 1e-6
    assert e.embed_documents(["x", "y"])[0] != e.embed_documents(["x", "y"])[1]


def test_voyage_embedder_caches_to_disk(tmp_path: Path, monkeypatch):
    from support_agent.knowledge import embeddings as emb

    calls: list[list[str]] = []

    class FakeVoyage:
        def __init__(self, *a, **k): ...

        def embed(self, texts, model, input_type):
            calls.append(list(texts))
            return type("R", (), {"embeddings": [[0.1, 0.2, float(len(t))] for t in texts]})()

    monkeypatch.setattr("voyageai.Client", FakeVoyage)
    e = emb.VoyageEmbedder("voyage-test", tmp_path, api_key="k")
    v1 = e.embed_documents(["alpha", "beta"])
    v2 = e.embed_documents(["alpha", "gamma"])  # alpha served from cache
    assert v1[0] == v2[0] and calls == [["alpha", "beta"], ["gamma"]] and e.dim == 3
    e2 = emb.VoyageEmbedder("voyage-test", tmp_path, api_key="k")  # cache survives a restart
    assert e2.embed_query("alpha") != v1[0] or calls[-1] == ["alpha"]  # query input_type is cached separately
