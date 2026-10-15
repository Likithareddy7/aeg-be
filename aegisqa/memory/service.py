"""The hybrid memory layer.

Every item is written to the exact store (MemoryRecord in PostgreSQL / Cloud
SQL) -- the source of truth, queryable by id, module and error code -- and, if
configured, mirrored to:

  semantic store   pgvector (same table) or Vertex AI Vector Search
  keyword store    PostgreSQL full-text (same table) or Firestore BM25

`search` runs keyword and semantic retrieval and merges them with reciprocal
rank fusion. Writes to the external stores are best effort: if one is down,
PostgreSQL still has the record and search falls back to what's reachable.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db import models as m
from ..db.base import new_id, session_scope, utcnow
from .bm25 import Bm25Index, FirestoreBm25Storage, tokenize
from .embeddings import Embedder, NoEmbedder, VertexEmbedder
from .vector_search import VectorSearchClient

logger = logging.getLogger(__name__)

RRF_K = 60
KINDS = ("test_case", "run_result", "healing", "defect")


@dataclass
class MemoryItem:
  project_id: str
  kind: str
  ref_id: str
  title: str
  content: str
  module: Optional[str] = None
  error_code: Optional[str] = None
  metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryHit:
  id: str
  kind: str
  ref_id: str
  title: str
  content: str
  module: Optional[str]
  error_code: Optional[str]
  metadata: dict[str, Any]
  score: float
  matched_by: list[str]

  def as_dict(self, snippet_chars: int = 400) -> dict[str, Any]:
    return {
      "id": self.id,
      "kind": self.kind,
      "refId": self.ref_id,
      "title": self.title,
      "snippet": self.content[:snippet_chars],
      "module": self.module,
      "errorCode": self.error_code,
      "metadata": self.metadata,
      "score": round(self.score, 6),
      "matchedBy": self.matched_by,
    }


def _hash(item: MemoryItem) -> str:
  return hashlib.sha256(f"{item.title}\n{item.content}".encode()).hexdigest()


def _hit(row: m.MemoryRecord, score: float = 0.0, matched_by: Optional[list[str]] = None) -> MemoryHit:
  return MemoryHit(
    id=row.id,
    kind=row.kind,
    ref_id=row.ref_id,
    title=row.title,
    content=row.content,
    module=row.module,
    error_code=row.error_code,
    metadata=json.loads(row.metadata_ or "{}"),
    score=score,
    matched_by=matched_by or [],
  )


class MemoryService:
  def __init__(
    self,
    *,
    embedder: Embedder,
    semantic_backend: str = "pgvector",
    keyword_backend: str = "postgres",
    vector_search: Optional[VectorSearchClient] = None,
    bm25: Optional[Bm25Index] = None,
  ):
    if semantic_backend not in ("pgvector", "vertex_vector_search", "none"):
      raise ValueError(f"Unknown MEMORY_SEMANTIC_BACKEND {semantic_backend!r}")
    if keyword_backend not in ("postgres", "firestore"):
      raise ValueError(f"Unknown MEMORY_KEYWORD_BACKEND {keyword_backend!r}")
    if semantic_backend == "vertex_vector_search" and vector_search is None:
      raise ValueError("vertex_vector_search needs a VectorSearchClient")
    if keyword_backend == "firestore" and bm25 is None:
      raise ValueError("firestore keyword search needs a Bm25Index")
    self.embedder = embedder
    self.semantic_backend = semantic_backend
    self.keyword_backend = keyword_backend
    self.vector_search = vector_search
    self.bm25 = bm25

  # -- writes -----------------------------------------------------------------

  async def index(self, item: MemoryItem) -> None:
    content_hash = _hash(item)
    async with session_scope() as db:
      row = await self._get(db, item.project_id, item.kind, item.ref_id)
      changed = row is None or row.content_hash != content_hash
      embedding = row.embedding if row is not None and not changed else None
      if (changed or embedding is None) and self.semantic_backend != "none":
        embedding = await self.embedder.embed(f"{item.title}\n{item.content}")
      if row is None:
        row = m.MemoryRecord(id=new_id(), project_id=item.project_id, kind=item.kind, ref_id=item.ref_id)
        db.add(row)
      row.title, row.content, row.module, row.error_code = item.title, item.content, item.module, item.error_code
      row.metadata_ = json.dumps(item.metadata, default=str)
      row.content_hash = content_hash
      if embedding is not None:
        row.embedding = list(embedding)
      row.updated_at = utcnow()
      await db.flush()
      record_id, vector = row.id, (list(row.embedding) if row.embedding is not None else None)

    if self.semantic_backend == "vertex_vector_search" and vector is not None and self.vector_search is not None:
      try:
        await self.vector_search.upsert(record_id, vector, item.project_id, item.kind)
      except Exception as err:  # noqa: BLE001 -- PostgreSQL keeps the record
        logger.warning("memory: Vector Search upsert failed for %s: %s", record_id, err)
    if self.keyword_backend == "firestore" and changed and self.bm25 is not None:
      try:
        await self.bm25.upsert(record_id, item.project_id, item.kind, item.ref_id, f"{item.title}\n{item.content}")
      except Exception as err:  # noqa: BLE001
        logger.warning("memory: Firestore BM25 upsert failed for %s: %s", record_id, err)

  async def remove(self, project_id: str, kind: str, ref_id: str) -> None:
    async with session_scope() as db:
      row = await self._get(db, project_id, kind, ref_id)
      if row is None:
        return
      record_id = row.id
      await db.delete(row)
    if self.semantic_backend == "vertex_vector_search" and self.vector_search is not None:
      try:
        await self.vector_search.remove([record_id])
      except Exception as err:  # noqa: BLE001
        logger.warning("memory: Vector Search remove failed for %s: %s", record_id, err)
    if self.keyword_backend == "firestore" and self.bm25 is not None:
      try:
        await self.bm25.remove(record_id)
      except Exception as err:  # noqa: BLE001
        logger.warning("memory: Firestore BM25 remove failed for %s: %s", record_id, err)

  @staticmethod
  async def _get(db: AsyncSession, project_id: str, kind: str, ref_id: str) -> Optional[m.MemoryRecord]:
    return (
      await db.execute(
        select(m.MemoryRecord).where(
          m.MemoryRecord.project_id == project_id, m.MemoryRecord.kind == kind, m.MemoryRecord.ref_id == ref_id
        )
      )
    ).scalar_one_or_none()

  # -- reads ------------------------------------------------------------------

  async def lookup(
    self,
    project_id: str,
    *,
    kind: Optional[str] = None,
    ref_id: Optional[str] = None,
    module: Optional[str] = None,
    error_code: Optional[str] = None,
    limit: int = 20,
  ) -> list[MemoryHit]:
    """Exact-store lookup by id, module or error code."""
    query = select(m.MemoryRecord).where(m.MemoryRecord.project_id == project_id)
    if kind:
      query = query.where(m.MemoryRecord.kind == kind)
    if ref_id:
      query = query.where(m.MemoryRecord.ref_id == ref_id)
    if module:
      query = query.where(m.MemoryRecord.module.ilike(module))
    if error_code:
      query = query.where(m.MemoryRecord.error_code == error_code)
    async with session_scope() as db:
      rows = (await db.execute(query.order_by(m.MemoryRecord.updated_at.desc()).limit(limit))).scalars()
      return [_hit(row, matched_by=["exact"]) for row in rows]

  async def search(self, project_id: str, query: str, kinds: Optional[list[str]] = None, limit: int = 10) -> list[MemoryHit]:
    keyword = await self._keyword_search(project_id, query, kinds, limit * 3)
    semantic = await self._semantic_search(project_id, query, kinds, limit * 3)
    fused: dict[str, float] = {}
    sources: dict[str, list[str]] = {}
    for name, ranked in (("keyword", keyword), ("semantic", semantic)):
      for rank, record_id in enumerate(ranked):
        fused[record_id] = fused.get(record_id, 0.0) + 1.0 / (RRF_K + rank + 1)
        sources.setdefault(record_id, []).append(name)
    if not fused:
      return []
    top = sorted(fused, key=lambda rid: fused[rid], reverse=True)[:limit]
    async with session_scope() as db:
      rows = {row.id: row for row in (await db.execute(select(m.MemoryRecord).where(m.MemoryRecord.id.in_(top)))).scalars()}
    return [_hit(rows[rid], fused[rid], sources[rid]) for rid in top if rid in rows]

  async def _keyword_search(self, project_id: str, query: str, kinds: Optional[list[str]], limit: int) -> list[str]:
    if self.keyword_backend == "firestore" and self.bm25 is not None:
      try:
        return [key for key, _ in await self.bm25.search(project_id, query, kinds, limit)]
      except Exception as err:  # noqa: BLE001 -- fall back to PostgreSQL full-text
        logger.warning("memory: Firestore BM25 search failed, using PostgreSQL: %s", err)
    terms = list(dict.fromkeys(tokenize(query)))[:30]
    if not terms:
      return []
    sql = """
      SELECT id FROM "MemoryRecord", to_tsquery('english', :query) AS q
      WHERE "projectId" = :project_id
        AND to_tsvector('english', title || ' ' || content) @@ q
        {kind_filter}
      ORDER BY ts_rank(to_tsvector('english', title || ' ' || content), q) DESC
      LIMIT :limit
    """.format(kind_filter='AND kind = ANY(:kinds)' if kinds else "")
    params: dict[str, Any] = {"query": " | ".join(terms), "project_id": project_id, "limit": limit}
    if kinds:
      params["kinds"] = list(kinds)
    async with session_scope() as db:
      return [row.id for row in await db.execute(text(sql), params)]

  async def _semantic_search(self, project_id: str, query: str, kinds: Optional[list[str]], limit: int) -> list[str]:
    if self.semantic_backend == "none":
      return []
    vector = await self.embedder.embed(query, query=True)
    if vector is None:
      return []
    if self.semantic_backend == "vertex_vector_search" and self.vector_search is not None:
      try:
        return [rid for rid, _ in await self.vector_search.query(vector, project_id, kinds, limit)]
      except Exception as err:  # noqa: BLE001
        logger.warning("memory: Vector Search query failed: %s", err)
        return []
    statement = (
      select(m.MemoryRecord.id)
      .where(m.MemoryRecord.project_id == project_id, m.MemoryRecord.embedding.is_not(None))
      .order_by(m.MemoryRecord.embedding.cosine_distance(vector))
      .limit(limit)
    )
    if kinds:
      statement = statement.where(m.MemoryRecord.kind.in_(kinds))
    async with session_scope() as db:
      return list((await db.execute(statement)).scalars())


def build_memory_service(settings: Settings, embedder: Optional[Embedder] = None) -> MemoryService:
  """From MEMORY_* settings. Locally (defaults): PostgreSQL for everything."""
  if embedder is None:
    embedder = NoEmbedder() if settings.memory_semantic_backend == "none" else VertexEmbedder(settings)
  vector_search = VectorSearchClient(settings) if settings.memory_semantic_backend == "vertex_vector_search" else None
  bm25 = (
    Bm25Index(FirestoreBm25Storage(settings.google_cloud_project, settings.firestore_database, settings.firestore_collection_prefix))
    if settings.memory_keyword_backend == "firestore"
    else None
  )
  return MemoryService(
    embedder=embedder,
    semantic_backend=settings.memory_semantic_backend,
    keyword_backend=settings.memory_keyword_backend,
    vector_search=vector_search,
    bm25=bm25,
  )
