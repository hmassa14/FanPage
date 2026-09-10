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


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


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
    def __init__(self, kb_dir: Path, k1: float = 1.5, b: float = 0.75) -> None:
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

    def search(self, query: str, top_k: int = 4) -> list[tuple[Chunk, float]]:
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

    def doc_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for c in self.chunks:
            seen.setdefault(c.doc_id, None)
        return list(seen)
