"""Memory layer pieces that need no database: BM25 (in-memory and against a
fake of the Firestore client calls it makes), error classification, and the
Vertex AI Vector Search REST calls."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, AsyncIterator

import httpx
import pytest

from aegisqa.config import settings
from aegisqa.memory.bm25 import Bm25Index, FirestoreBm25Storage, InMemoryBm25Storage, tokenize
from aegisqa.memory.learning import classify_error
from aegisqa.memory.vector_search import VectorSearchClient

DOCS = {
  "d1": "Login rejects a wrong password and shows an error message",
  "d2": "Checkout totals include tax and shipping",
  "d3": "Login with valid credentials opens the dashboard",
  "d4": "Password reset email is sent",
}


async def _seed(index: Bm25Index) -> None:
  for key, text in DOCS.items():
    await index.upsert(key, "p1", "test_case", key, text)
  await index.upsert("other", "p2", "test_case", "other", "login wrong password")  # another project


def test_tokenize_drops_stopwords_and_punctuation() -> None:
  assert tokenize("The user should see: Login-Error 404!") == ["login", "error", "404"]


async def test_bm25_ranks_and_scopes_by_project_and_kind() -> None:
  index = Bm25Index(InMemoryBm25Storage())
  await _seed(index)
  ranked = await index.search("p1", "wrong password login")
  assert [key for key, _ in ranked][:2] == ["d1", "d3"] or [key for key, _ in ranked][0] == "d1"
  assert "other" not in [key for key, _ in ranked]
  assert await index.search("p1", "login", kinds=["defect"]) == []
  assert await index.search("p1", "the and of") == []


async def test_bm25_updates_and_deletes_keep_statistics_right() -> None:
  storage = InMemoryBm25Storage()
  index = Bm25Index(storage)
  await _seed(index)
  assert await storage.stats("p1") == (4, sum(len(tokenize(t)) for t in DOCS.values()))
  assert (await storage.document_frequencies("p1", ["login"]))["login"] == 2

  await index.upsert("d1", "p1", "test_case", "d1", "Checkout coupon codes")  # rewritten
  assert (await storage.document_frequencies("p1", ["login", "checkout"])) == {"login": 1, "checkout": 2}
  assert (await storage.stats("p1"))[0] == 4

  await index.remove("d2")
  assert (await storage.document_frequencies("p1", ["checkout"]))["checkout"] == 1
  assert (await storage.stats("p1"))[0] == 3
  assert [k for k, _ in await index.search("p1", "checkout")] == ["d1"]


# --- A fake of the Firestore AsyncClient surface FirestoreBm25Storage uses ---


class _Snapshot:
  def __init__(self, doc_id: str, data: Any):
    self.id, self._data = doc_id, data

  @property
  def exists(self) -> bool:
    return self._data is not None

  def to_dict(self) -> Any:
    return dict(self._data) if self._data is not None else None


class _DocRef:
  def __init__(self, store: dict[str, dict[str, Any]], collection: str, doc_id: str):
    self._store, self.collection, self.id = store, collection, doc_id

  async def get(self) -> _Snapshot:
    return _Snapshot(self.id, self._store.get(f"{self.collection}/{self.id}"))


class _Query:
  def __init__(self, store: dict[str, dict[str, Any]], collection: str, filters: list[Any], limit: int | None = None):
    self._store, self._collection, self._filters, self._limit = store, collection, filters, limit

  def where(self, *, filter: Any) -> "_Query":  # noqa: A002 -- Firestore's keyword
    return _Query(self._store, self._collection, [*self._filters, filter], self._limit)

  def limit(self, n: int) -> "_Query":
    return _Query(self._store, self._collection, self._filters, n)

  def _matches(self, data: dict[str, Any]) -> bool:
    for f in self._filters:
      value = data.get(f.field_path)
      if f.op_string == "==" and value != f.value:
        return False
      if f.op_string == "array_contains_any" and not set(value or []) & set(f.value):
        return False
    return True

  async def stream(self) -> AsyncIterator[_Snapshot]:
    count = 0
    for path, data in self._store.items():
      collection, doc_id = path.split("/", 1)
      if collection == self._collection and self._matches(data):
        yield _Snapshot(doc_id, data)
        count += 1
        if self._limit and count >= self._limit:
          return


class _Collection(_Query):
  def document(self, doc_id: str) -> _DocRef:
    return _DocRef(self._store, self._collection, doc_id)


class _Batch:
  def __init__(self, store: dict[str, dict[str, Any]]):
    self._store, self._ops = store, []

  def set(self, ref: _DocRef, data: dict[str, Any], merge: bool = False) -> None:
    self._ops.append(("set", ref, data, merge))

  def delete(self, ref: _DocRef) -> None:
    self._ops.append(("delete", ref, None, False))

  async def commit(self) -> None:
    from google.cloud.firestore_v1.transforms import Increment

    for op, ref, data, merge in self._ops:
      path = f"{ref.collection}/{ref.id}"
      if op == "delete":
        self._store.pop(path, None)
        continue
      current = dict(self._store.get(path, {})) if merge else {}
      for key, value in data.items():
        current[key] = current.get(key, 0) + value.value if isinstance(value, Increment) else value
      self._store[path] = current


class FakeFirestore:
  def __init__(self) -> None:
    self.store: dict[str, dict[str, Any]] = {}

  def collection(self, name: str) -> _Collection:
    return _Collection(self.store, name, [])

  def batch(self) -> _Batch:
    return _Batch(self.store)

  async def get_all(self, refs: list[_DocRef]) -> AsyncIterator[_Snapshot]:
    for ref in refs:
      yield await ref.get()


async def test_firestore_storage_matches_in_memory_behaviour() -> None:
  fake = FakeFirestore()
  index = Bm25Index(FirestoreBm25Storage("proj", "(default)", "aegisqa", client=fake))
  await _seed(index)
  ranked = await index.search("p1", "wrong password login")
  assert ranked[0][0] == "d1" and "other" not in [k for k, _ in ranked]

  storage = index.storage
  assert await storage.stats("p1") == (4, sum(len(tokenize(t)) for t in DOCS.values()))
  await index.upsert("d1", "p1", "test_case", "d1", "Checkout coupon codes")
  await index.remove("d2")
  assert await storage.document_frequencies("p1", ["login", "checkout"]) == {"login": 1, "checkout": 1}
  assert (await storage.stats("p1"))[0] == 3
  doc = fake.store["aegisqa_bm25_docs/d1"]
  assert doc["projectId"] == "p1" and doc["terms"] == ["checkout", "codes", "coupon"]


def test_classify_error() -> None:
  assert classify_error('Could not locate element "Sign in" on the page') == "ELEMENT_NOT_FOUND"
  assert classify_error("page.goto: net::ERR_NAME_NOT_RESOLVED at https://x") == "ERR_NAME_NOT_RESOLVED"
  assert classify_error('Found request(s) matching "/api" but none had status 200') == "HTTP_200"
  assert classify_error('Expected page to contain text "Welcome"') == "ASSERTION_FAILED"
  assert classify_error("Timeout 5000ms exceeded") == "TIMEOUT"
  assert classify_error("something odd") == "STEP_FAILED"


async def test_vector_search_rest_calls() -> None:
  calls: list[httpx.Request] = []

  def handler(request: httpx.Request) -> httpx.Response:
    calls.append(request)
    if request.url.path.endswith(":findNeighbors"):
      return httpx.Response(200, json={"nearestNeighbors": [{"neighbors": [{"datapoint": {"datapointId": "rec-1"}, "distance": 0.12}]}]})
    return httpx.Response(200, json={})

  async def token() -> str:
    return "tok"

  client = VectorSearchClient(
    replace(
      settings,
      vector_search_index="projects/p/locations/us-central1/indexes/42",
      vector_search_index_endpoint="projects/p/locations/us-central1/indexEndpoints/7",
      vector_search_deployed_index_id="aegisqa_memory",
      vector_search_endpoint_domain="123.us-central1-456.vdb.vertexai.goog",
    ),
    token_provider=token,
    transport=httpx.MockTransport(handler),
  )
  await client.upsert("rec-1", [0.5] * 3, "proj-1", "test_case")
  hits = await client.query([0.5] * 3, "proj-1", ["test_case"], 5)
  await client.remove(["rec-1"])

  upsert, query, remove = calls
  assert str(upsert.url) == "https://us-central1-aiplatform.googleapis.com/v1/projects/p/locations/us-central1/indexes/42:upsertDatapoints"
  assert upsert.headers["Authorization"] == "Bearer tok"
  assert json.loads(upsert.content)["datapoints"][0]["restricts"] == [
    {"namespace": "project", "allowList": ["proj-1"]},
    {"namespace": "kind", "allowList": ["test_case"]},
  ]
  assert str(query.url) == "https://123.us-central1-456.vdb.vertexai.goog/v1/projects/p/locations/us-central1/indexEndpoints/7:findNeighbors"
  body = json.loads(query.content)
  assert body["deployedIndexId"] == "aegisqa_memory" and body["queries"][0]["neighborCount"] == 5
  assert hits == [("rec-1", 0.12)]
  assert json.loads(remove.content) == {"datapointIds": ["rec-1"]}


def test_vector_search_requires_configuration() -> None:
  with pytest.raises(ValueError, match="VECTOR_SEARCH_INDEX"):
    VectorSearchClient(replace(settings, vector_search_index=""))
