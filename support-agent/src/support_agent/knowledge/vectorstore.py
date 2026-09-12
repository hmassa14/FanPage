"""Weaviate index for hybrid retrieval.

Weaviate does the fusion: `collection.query.hybrid(query, vector, alpha)` runs its own BM25
over the text property and a vector search over the supplied embedding, then merges with
relative-score fusion. `alpha` = 0 is pure keyword, 1 is pure vector.

Vectors are self-provided (computed by our `Embedder`), so the same code runs against
embedded Weaviate (dev, tests), a compose container, or Weaviate Cloud, and the embedding
provider is our choice rather than a server-side module.

Sync is idempotent: object ids are uuid5(ref), a content hash is stored on each object,
and only changed or missing chunks are written. Start-up on an unchanged corpus is a read.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import uuid
from typing import Any

import weaviate
from weaviate.classes.config import Configure, DataType, Property, Tokenization
from weaviate.classes.query import HybridFusion, MetadataQuery

from ..config import Settings
from .embeddings import Embedder
from .kb import Chunk

logger = logging.getLogger(__name__)
NAMESPACE = uuid.UUID("2f1b3d1c-6d2a-4b8e-9c1e-5a7f0e3c9d11")


def _chunk_hash(c: Chunk, embedder_name: str) -> str:
    return hashlib.sha256(f"{embedder_name}|{c.doc_id}|{c.title}|{c.section}|{c.text}".encode()).hexdigest()


class WeaviateIndex:
    def __init__(self, s: Settings, collection: str = "PolicyChunk") -> None:
        self.s = s
        self.collection_name = collection
        if s.weaviate_mode == "embedded":
            s.weaviate_data_dir.mkdir(parents=True, exist_ok=True)
            self.client = weaviate.connect_to_embedded(
                persistence_data_path=str(s.weaviate_data_dir),
                port=s.weaviate_embedded_port,
                grpc_port=s.weaviate_embedded_grpc_port,
                environment_variables={
                    # The sandbox this was built in has no private IP; Weaviate's memberlist
                    # needs an explicit advertise address to start single-node.
                    "CLUSTER_ADVERTISE_ADDR": "127.0.0.1",
                    "DISABLE_TELEMETRY": "true",
                    "LOG_LEVEL": "error",
                },
            )
        else:
            from urllib.parse import urlparse

            u = urlparse(s.weaviate_url)
            self.client = weaviate.connect_to_custom(
                http_host=u.hostname or "localhost",
                http_port=u.port or (443 if u.scheme == "https" else 8080),
                http_secure=u.scheme == "https",
                grpc_host=s.weaviate_grpc_host or (u.hostname or "localhost"),
                grpc_port=s.weaviate_grpc_port,
                grpc_secure=u.scheme == "https",
                auth_credentials=weaviate.classes.init.Auth.api_key(s.weaviate_api_key)
                if s.weaviate_api_key
                else None,
            )
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        if not self.client.collections.exists(self.collection_name):
            self.client.collections.create(
                self.collection_name,
                vector_config=Configure.Vectors.self_provided(),
                properties=[
                    Property(name="ref", data_type=DataType.TEXT, tokenization=Tokenization.FIELD),
                    Property(name="doc_id", data_type=DataType.TEXT, tokenization=Tokenization.FIELD),
                    Property(name="title", data_type=DataType.TEXT),
                    Property(name="section", data_type=DataType.TEXT),
                    Property(name="text", data_type=DataType.TEXT, tokenization=Tokenization.WORD),
                    Property(
                        name="content_hash",
                        data_type=DataType.TEXT,
                        tokenization=Tokenization.FIELD,
                        skip_vectorization=True,
                    ),
                ],
            )
        self.collection = self.client.collections.get(self.collection_name)

    # ---- sync ------------------------------------------------------------------- #
    def sync(self, chunks: list[Chunk], embedder: Embedder) -> dict[str, int]:
        existing: dict[str, str] = {}
        for obj in self.collection.iterator(return_properties=["ref", "content_hash"]):
            existing[str(obj.properties["ref"])] = str(obj.properties.get("content_hash") or "")
        wanted = {c.ref: (c, _chunk_hash(c, embedder.name)) for c in chunks}
        to_write = [c for ref, (c, h) in wanted.items() if existing.get(ref) != h]
        stale = [ref for ref in existing if ref not in wanted]
        if to_write:
            vectors = embedder.embed_documents([f"{c.title}\n{c.section}\n{c.text}" for c in to_write])
            with self.collection.batch.fixed_size(batch_size=50) as batch:
                for c, vec in zip(to_write, vectors, strict=True):
                    batch.add_object(
                        properties={
                            "ref": c.ref,
                            "doc_id": c.doc_id,
                            "title": c.title,
                            "section": c.section,
                            "text": c.text,
                            "content_hash": wanted[c.ref][1],
                        },
                        uuid=uuid.uuid5(NAMESPACE, c.ref),
                        vector=vec,
                    )
            if self.collection.batch.failed_objects:
                raise RuntimeError(f"weaviate batch failed: {self.collection.batch.failed_objects[:3]}")
        for ref in stale:
            self.collection.data.delete_by_id(uuid.uuid5(NAMESPACE, ref))
        stats = {"written": len(to_write), "deleted": len(stale), "unchanged": len(wanted) - len(to_write)}
        logger.info("weaviate sync %s", stats)
        return stats

    # ---- query ------------------------------------------------------------------ #
    def hybrid(self, query: str, vector: list[float], alpha: float, top_k: int) -> list[tuple[str, float]]:
        res = self.collection.query.hybrid(
            query=query,
            vector=vector,
            alpha=alpha,
            limit=top_k,
            query_properties=["text", "section", "title"],
            fusion_type=HybridFusion.RELATIVE_SCORE,
            return_metadata=MetadataQuery(score=True),
            return_properties=["ref"],
        )
        return [(str(o.properties["ref"]), float(o.metadata.score or 0.0)) for o in res.objects]

    def count(self) -> int:
        return int(self.collection.aggregate.over_all(total_count=True).total_count or 0)

    def close(self) -> None:
        with contextlib.suppress(Exception):  # closing is best-effort
            self.client.close()

    def __enter__(self) -> WeaviateIndex:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
