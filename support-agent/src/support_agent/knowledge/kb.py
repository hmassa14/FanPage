"""Knowledge base retrieval: markdown docs, chunked by section, scored with BM25.

BM25 is deliberate for a first production cut: no embedding service to run, fully
deterministic, easy to unit-test, and good enough for a policy corpus of a few hundred
sections. Swap `KnowledgeBase.search` for hybrid/vector retrieval later without touching
the pipeline: the `Chunk` contract stays the same.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .embeddings import Embedder
    from .vectorstore import WeaviateIndex

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "in",
    "is",
    "are",
    "be",
    "for",
    "on",
    "with",
    "it",
    "this",
    "that",
    "by",
    "at",
    "as",
    "we",
    "our",
    "you",
    "your",
    "i",
    "my",
    "me",
    "can",
    "not",
    "from",
    "have",
    "has",
    "was",
    "were",
    "will",
    "do",
    "if",
    "may",
    "any",
}


def _stem(t: str) -> str:
    """Light suffix stripping. Enough to make 'returned', 'returns', 'returning' match 'return'
    without pulling in a stemming library. The retrieval eval is what justified adding it."""
    for suffix in ("ing", "ies", "ed", "es", "s"):
        if len(t) > len(suffix) + 3 and t.endswith(suffix):
            if suffix == "ies":
                return t[:-3] + "y"
            return t[: -len(suffix)]
    return t


def tokenize(text: str) -> list[str]:
    return [_stem(t) for t in _TOKEN.findall(text.lower()) if t not in _STOP]


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    title: str
    section: str
    text: str

    @property
    def ref(self) -> str:
        return f"{self.doc_id}#{self.section}"


def _parse_doc(path: Path) -> list[Chunk]:
    raw = path.read_text(encoding="utf-8")
    doc_id, title = path.stem, path.stem
    body = raw
    if raw.startswith("---"):
        _, fm, body = raw.split("---", 2)
        for line in fm.strip().splitlines():
            k, _, v = line.partition(":")
            if k.strip() == "id":
                doc_id = v.strip()
            elif k.strip() == "title":
                title = v.strip()
    chunks: list[Chunk] = []
    section: str = "Overview"
    buf: list[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            if buf and "".join(buf).strip():
                chunks.append(Chunk(doc_id, title, section, "\n".join(buf).strip()))
            section, buf = line[3:].strip(), []
        else:
            buf.append(line)
    if buf and "".join(buf).strip():
        chunks.append(Chunk(doc_id, title, section, "\n".join(buf).strip()))
    return chunks


class KnowledgeBase:
    def __init__(
        self,
        kb_dir: Path,
        k1: float = 1.5,
        b: float = 0.75,
        *,
        retriever: Literal["bm25", "hybrid"] = "bm25",
        index: WeaviateIndex | None = None,
        embedder: Embedder | None = None,
        alpha: float = 0.5,
    ) -> None:
        self.retriever = retriever
        self.index, self.embedder, self.alpha = index, embedder, alpha
        if retriever == "hybrid" and not (index and embedder):
            raise ValueError("hybrid retrieval needs a WeaviateIndex and an Embedder")
        self.chunks: list[Chunk] = []
        for path in sorted(kb_dir.glob("*.md")):
            self.chunks.extend(_parse_doc(path))
        if not self.chunks:
            raise ValueError(f"No knowledge base documents found in {kb_dir}")
        self.k1, self.b = k1, b
        self._docs = [tokenize(f"{c.title} {c.section} {c.text}") for c in self.chunks]
        self._avgdl = sum(len(d) for d in self._docs) / len(self._docs)
        self._tf = [Counter(d) for d in self._docs]
        df: Counter[str] = Counter()
        for d in self._docs:
            df.update(set(d))
        n = len(self._docs)
        self._idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

        self._by_ref = {c.ref: c for c in self.chunks}
        if self.index and self.embedder:
            self.index.sync(self.chunks, self.embedder)

    @property
    def retriever_name(self) -> str:
        if self.retriever == "hybrid" and self.embedder:
            return f"hybrid(weaviate, {self.embedder.name}, alpha={self.alpha})"
        return "bm25"

    def search(self, query: str, top_k: int = 4) -> list[tuple[Chunk, float]]:
        if self.retriever == "hybrid":
            return self.search_hybrid(query, top_k)
        return self.search_bm25(query, top_k)

    def search_hybrid(
        self, query: str, top_k: int = 4, alpha: float | None = None
    ) -> list[tuple[Chunk, float]]:
        assert self.index and self.embedder
        hits = self.index.hybrid(
            query, self.embedder.embed_query(query), self.alpha if alpha is None else alpha, top_k
        )
        return [(self._by_ref[ref], score) for ref, score in hits if ref in self._by_ref]

    def search_bm25(self, query: str, top_k: int = 4) -> list[tuple[Chunk, float]]:
        q = tokenize(query)
        scores: list[tuple[Chunk, float]] = []
        for i, chunk in enumerate(self.chunks):
            tf, dl = self._tf[i], len(self._docs[i])
            s = 0.0
            for t in q:
                if t not in tf:
                    continue
                f = tf[t]
                s += (
                    self._idf[t]
                    * (f * (self.k1 + 1))
                    / (f + self.k1 * (1 - self.b + self.b * dl / self._avgdl))
                )
            if s > 0:
                scores.append((chunk, s))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]

    def get_doc(self, doc_id: str) -> list[Chunk]:
        return [c for c in self.chunks if c.doc_id == doc_id]

    def close(self) -> None:
        if self.index:
            self.index.close()

    def doc_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for c in self.chunks:
            seen.setdefault(c.doc_id, None)
        return list(seen)
