"""BM25 keyword index -- the "BM25 metadata (Firestore)" store.

Firestore has no full-text ranking, so the index is kept by hand:

  {prefix}_bm25_docs/{docKey}          projectId, kind, refId, terms[], tf{term: n}, length
  {prefix}_bm25_terms/{project}__{term} df  (how many of the project's docs contain the term)
  {prefix}_bm25_stats/{project}        docCount, totalLength

A query fetches candidate docs that contain any query term
(`array-contains-any`, max 30 terms) and ranks them with BM25 using the
stored document frequencies. It needs one composite index, on
(projectId ASC, terms ARRAY_CONTAINS) -- see docs/GCP_HANDOFF.md.

`InMemoryBm25Storage` implements the same storage contract for tests.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

K1 = 1.2
B = 0.75
MAX_QUERY_TERMS = 30  # Firestore's array-contains-any limit

_STOPWORDS = {
  "the", "and", "for", "with", "that", "this", "when", "then", "should", "must", "can", "will", "are", "was",
  "from", "into", "user", "users", "able", "have", "has", "not", "all", "any", "their", "they", "them", "its",
  "given", "page", "see", "shown", "show", "displayed", "display", "via", "you", "your", "our",
}


def tokenize(text: str) -> list[str]:
  return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1 and w not in _STOPWORDS]


@dataclass
class Bm25Doc:
  key: str
  project_id: str
  kind: str
  ref_id: str
  tf: dict[str, int] = field(default_factory=dict)
  length: int = 0


class Bm25Storage(Protocol):
  async def get_doc(self, key: str) -> Optional[Bm25Doc]: ...
  async def write_doc(self, doc: Bm25Doc, previous: Optional[Bm25Doc]) -> None: ...
  async def delete_doc(self, doc: Bm25Doc) -> None: ...
  async def candidates(self, project_id: str, terms: list[str], kinds: Optional[list[str]], limit: int) -> list[Bm25Doc]: ...
  async def document_frequencies(self, project_id: str, terms: list[str]) -> dict[str, int]: ...
  async def stats(self, project_id: str) -> tuple[int, int]: ...


def score(query_terms: list[str], doc: Bm25Doc, dfs: dict[str, int], doc_count: int, avg_length: float) -> float:
  total = 0.0
  for term in set(query_terms):
    freq = doc.tf.get(term, 0)
    if not freq:
      continue
    df = max(1, dfs.get(term, 1))
    idf = math.log(1 + (doc_count - df + 0.5) / (df + 0.5))
    norm = freq + K1 * (1 - B + B * doc.length / (avg_length or 1))
    total += idf * freq * (K1 + 1) / norm
  return total


class Bm25Index:
  def __init__(self, storage: Bm25Storage):
    self.storage = storage

  async def upsert(self, key: str, project_id: str, kind: str, ref_id: str, text: str) -> None:
    terms = tokenize(text)
    doc = Bm25Doc(key=key, project_id=project_id, kind=kind, ref_id=ref_id, tf=dict(Counter(terms)), length=len(terms))
    await self.storage.write_doc(doc, await self.storage.get_doc(key))

  async def remove(self, key: str) -> None:
    doc = await self.storage.get_doc(key)
    if doc is not None:
      await self.storage.delete_doc(doc)

  async def search(self, project_id: str, query: str, kinds: Optional[list[str]] = None, limit: int = 10) -> list[tuple[str, float]]:
    terms = list(dict.fromkeys(tokenize(query)))[:MAX_QUERY_TERMS]
    if not terms:
      return []
    docs = await self.storage.candidates(project_id, terms, kinds, limit=max(50, limit * 5))
    if not docs:
      return []
    dfs = await self.storage.document_frequencies(project_id, terms)
    doc_count, total_length = await self.storage.stats(project_id)
    avg = total_length / doc_count if doc_count else 1.0
    ranked = sorted(((d.key, score(terms, d, dfs, max(doc_count, len(docs)), avg)) for d in docs), key=lambda x: x[1], reverse=True)
    return [(key, s) for key, s in ranked if s > 0][:limit]


# ---------------------------------------------------------------------------
# Storage implementations
# ---------------------------------------------------------------------------


def _delta(doc: Bm25Doc, previous: Optional[Bm25Doc]) -> tuple[dict[str, int], int, int]:
  """Changes to document frequencies, doc count and total length."""
  old_terms = set(previous.tf) if previous else set()
  new_terms = set(doc.tf)
  df = {t: 1 for t in new_terms - old_terms}
  df.update({t: -1 for t in old_terms - new_terms})
  return df, (0 if previous else 1), doc.length - (previous.length if previous else 0)


class InMemoryBm25Storage:
  def __init__(self) -> None:
    self.docs: dict[str, Bm25Doc] = {}
    self.df: dict[tuple[str, str], int] = {}
    self.totals: dict[str, list[int]] = {}

  def _apply(self, project_id: str, df: dict[str, int], count: int, length: int) -> None:
    for term, change in df.items():
      self.df[(project_id, term)] = self.df.get((project_id, term), 0) + change
    totals = self.totals.setdefault(project_id, [0, 0])
    totals[0] += count
    totals[1] += length

  async def get_doc(self, key: str) -> Optional[Bm25Doc]:
    return self.docs.get(key)

  async def write_doc(self, doc: Bm25Doc, previous: Optional[Bm25Doc]) -> None:
    self._apply(doc.project_id, *_delta(doc, previous))
    self.docs[doc.key] = doc

  async def delete_doc(self, doc: Bm25Doc) -> None:
    self._apply(doc.project_id, {t: -1 for t in doc.tf}, -1, -doc.length)
    self.docs.pop(doc.key, None)

  async def candidates(self, project_id: str, terms: list[str], kinds: Optional[list[str]], limit: int) -> list[Bm25Doc]:
    wanted = set(terms)
    hits = [
      d for d in self.docs.values() if d.project_id == project_id and wanted & set(d.tf) and (not kinds or d.kind in kinds)
    ]
    return hits[:limit]

  async def document_frequencies(self, project_id: str, terms: list[str]) -> dict[str, int]:
    return {t: self.df.get((project_id, t), 0) for t in terms}

  async def stats(self, project_id: str) -> tuple[int, int]:
    count, length = self.totals.get(project_id, [0, 0])
    return count, length


class FirestoreBm25Storage:
  def __init__(self, project: str, database: str, prefix: str, client: Any = None):
    if client is None:
      from google.cloud import firestore

      client = firestore.AsyncClient(project=project or None, database=database)
    self._client = client
    self._docs = client.collection(f"{prefix}_bm25_docs")
    self._terms = client.collection(f"{prefix}_bm25_terms")
    self._stats = client.collection(f"{prefix}_bm25_stats")

  @staticmethod
  def _doc_id(key: str) -> str:
    return re.sub(r"[/]", "_", key)

  @staticmethod
  def _term_id(project_id: str, term: str) -> str:
    return f"{project_id}__{term}"

  @staticmethod
  def _from_snapshot(key: str, data: dict[str, Any]) -> Bm25Doc:
    return Bm25Doc(
      key=data.get("key", key),
      project_id=data["projectId"],
      kind=data["kind"],
      ref_id=data["refId"],
      tf={k: int(v) for k, v in (data.get("tf") or {}).items()},
      length=int(data.get("length", 0)),
    )

  async def get_doc(self, key: str) -> Optional[Bm25Doc]:
    snapshot = await self._docs.document(self._doc_id(key)).get()
    return self._from_snapshot(key, snapshot.to_dict()) if snapshot.exists else None

  def _write_counters(self, batch: Any, project_id: str, df: dict[str, int], count: int, length: int) -> None:
    from google.cloud import firestore

    for term, change in df.items():
      batch.set(self._terms.document(self._term_id(project_id, term)), {"df": firestore.Increment(change)}, merge=True)
    if count or length:
      batch.set(
        self._stats.document(project_id),
        {"docCount": firestore.Increment(count), "totalLength": firestore.Increment(length)},
        merge=True,
      )

  async def write_doc(self, doc: Bm25Doc, previous: Optional[Bm25Doc]) -> None:
    batch = self._client.batch()
    batch.set(
      self._docs.document(self._doc_id(doc.key)),
      {
        "key": doc.key,
        "projectId": doc.project_id,
        "kind": doc.kind,
        "refId": doc.ref_id,
        "terms": sorted(doc.tf),
        "tf": doc.tf,
        "length": doc.length,
      },
    )
    self._write_counters(batch, doc.project_id, *_delta(doc, previous))
    await batch.commit()

  async def delete_doc(self, doc: Bm25Doc) -> None:
    batch = self._client.batch()
    batch.delete(self._docs.document(self._doc_id(doc.key)))
    self._write_counters(batch, doc.project_id, {t: -1 for t in doc.tf}, -1, -doc.length)
    await batch.commit()

  async def candidates(self, project_id: str, terms: list[str], kinds: Optional[list[str]], limit: int) -> list[Bm25Doc]:
    from google.cloud.firestore_v1.base_query import FieldFilter

    query = (
      self._docs.where(filter=FieldFilter("projectId", "==", project_id))
      .where(filter=FieldFilter("terms", "array_contains_any", terms[:MAX_QUERY_TERMS]))
      .limit(limit)
    )
    docs = [self._from_snapshot(s.id, s.to_dict()) async for s in query.stream()]
    return [d for d in docs if not kinds or d.kind in kinds]

  async def document_frequencies(self, project_id: str, terms: list[str]) -> dict[str, int]:
    refs = [self._terms.document(self._term_id(project_id, t)) for t in terms]
    out = {t: 0 for t in terms}
    async for snapshot in self._client.get_all(refs):
      if snapshot.exists:
        out[snapshot.id.split("__", 1)[1]] = int(snapshot.to_dict().get("df", 0))
    return out

  async def stats(self, project_id: str) -> tuple[int, int]:
    snapshot = await self._stats.document(project_id).get()
    data = snapshot.to_dict() if snapshot.exists else {}
    return int(data.get("docCount", 0)), int(data.get("totalLength", 0))
