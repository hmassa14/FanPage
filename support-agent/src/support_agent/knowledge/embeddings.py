"""Embedding providers for hybrid retrieval.

`VoyageEmbedder` is the real one (Anthropic's recommended embeddings partner). Vectors are
cached on disk keyed by model + input type + text, so re-indexing an unchanged corpus and
re-running the retrieval eval cost nothing.

`HashEmbedder` is a deterministic, key-free fallback: feature-hashed character trigrams,
L2-normalised. It is *lexical*, not semantic; it exists so the Weaviate plumbing, the sync
logic, and the eval harness run in CI. Its retrieval numbers are reported under its own
name and should not be mistaken for what Voyage produces.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from pathlib import Path
from typing import Any, Protocol

from ..config import Settings

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    name: str
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class HashEmbedder:
    def __init__(self, dim: int = 512) -> None:
        self.dim = dim
        self.name = f"hash-trigram-{dim}"

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        t = f"  {text.lower()}  "
        for i in range(len(t) - 2):
            tri = t[i : i + 3]
            h = int(hashlib.blake2b(tri.encode(), digest_size=4).hexdigest(), 16)
            v[h % self.dim] += 1.0 if (h >> 31) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


class VoyageEmbedder:
    def __init__(self, model: str, cache_dir: Path, api_key: str | None = None) -> None:
        import voyageai

        self.model, self.name = model, f"voyage:{model}"
        self.client = voyageai.Client(api_key=api_key) if api_key else voyageai.Client()
        self.cache_path = cache_dir / f"embeddings-{model}.json"
        self.cache: dict[str, list[float]] = {}
        if self.cache_path.exists():
            self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.dim = len(next(iter(self.cache.values()))) if self.cache else 0

    def _key(self, text: str, input_type: str) -> str:
        return hashlib.sha256(f"{self.model}|{input_type}|{text}".encode()).hexdigest()

    def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        keys = [self._key(t, input_type) for t in texts]
        missing = [(k, t) for k, t in zip(keys, texts, strict=True) if k not in self.cache]
        if missing:
            result = self.client.embed([t for _, t in missing], model=self.model, input_type=input_type)
            embeddings: list[Any] = list(result.embeddings)
            for (k, _), vec in zip(missing, embeddings, strict=True):
                self.cache[k] = [float(x) for x in vec]
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache), encoding="utf-8")
            logger.info("embedded %d texts with %s", len(missing), self.model)
        vecs = [self.cache[k] for k in keys]
        if vecs:
            self.dim = len(vecs[0])
        return vecs

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "document")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "query")[0]


def build_embedder(s: Settings) -> Embedder:
    if s.embedder == "voyage":
        return VoyageEmbedder(s.voyage_model, s.embedding_cache_dir, s.voyage_api_key)
    return HashEmbedder()
