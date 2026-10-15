"""Vertex AI Vector Search -- the "semantic store" in the target architecture.

Talks to the REST API directly (no extra SDK):
  upsert/remove   POST https://{region}-aiplatform.googleapis.com/v1/{index}:upsertDatapoints | :removeDatapoints
  query           POST https://{endpoint domain}/v1/{index endpoint}:findNeighbors

The index must use streaming updates, 768 dimensions and cosine distance; it's
created and deployed by the GCP engineer (docs/GCP_HANDOFF.md). Datapoint ids
are MemoryRecord ids, with `project` and `kind` restricts so a query only sees
one project's records.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Awaitable, Callable, Optional

import httpx

from ..config import Settings

TokenProvider = Callable[[], Awaitable[str]]


def _region(resource_name: str) -> str:
  match = re.search(r"/locations/([^/]+)/", resource_name)
  if not match:
    raise ValueError(f"Can't find the region in {resource_name!r} (expected projects/P/locations/REGION/...)")
  return match.group(1)


class GoogleTokens:
  """Access tokens from Application Default Credentials (the service account on GCP)."""

  def __init__(self) -> None:
    self._credentials: Any = None
    self._lock = asyncio.Lock()

  async def __call__(self) -> str:
    async with self._lock:
      if self._credentials is None:
        import google.auth

        self._credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
      if not self._credentials.valid or (self._credentials.expiry and self._credentials.expiry.timestamp() - time.time() < 60):
        from google.auth.transport.requests import Request

        await asyncio.to_thread(self._credentials.refresh, Request())
      return str(self._credentials.token)


class VectorSearchClient:
  def __init__(
    self,
    settings: Settings,
    token_provider: Optional[TokenProvider] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
  ):
    missing = [
      name
      for name, value in (
        ("VECTOR_SEARCH_INDEX", settings.vector_search_index),
        ("VECTOR_SEARCH_INDEX_ENDPOINT", settings.vector_search_index_endpoint),
        ("VECTOR_SEARCH_DEPLOYED_INDEX_ID", settings.vector_search_deployed_index_id),
        ("VECTOR_SEARCH_ENDPOINT_DOMAIN", settings.vector_search_endpoint_domain),
      )
      if not value
    ]
    if missing:
      raise ValueError(f"MEMORY_SEMANTIC_BACKEND=vertex_vector_search needs {', '.join(missing)}")
    self._index = settings.vector_search_index.strip("/")
    self._endpoint = settings.vector_search_index_endpoint.strip("/")
    self._deployed_index_id = settings.vector_search_deployed_index_id
    self._api = f"https://{_region(self._index)}-aiplatform.googleapis.com/v1"
    domain = settings.vector_search_endpoint_domain.rstrip("/")
    self._query_api = (domain if domain.startswith("http") else f"https://{domain}") + "/v1"
    self._tokens = token_provider or GoogleTokens()
    self._transport = transport

  async def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
    token = await self._tokens()
    async with httpx.AsyncClient(timeout=30, transport=self._transport) as client:
      response = await client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
    if response.status_code >= 400:
      raise RuntimeError(f"Vertex AI Vector Search error ({response.status_code}): {response.text[:500]}")
    return response.json() if response.content else {}

  async def upsert(self, datapoint_id: str, vector: list[float], project_id: str, kind: str) -> None:
    await self._post(
      f"{self._api}/{self._index}:upsertDatapoints",
      {
        "datapoints": [
          {
            "datapointId": datapoint_id,
            "featureVector": vector,
            "restricts": [
              {"namespace": "project", "allowList": [project_id]},
              {"namespace": "kind", "allowList": [kind]},
            ],
          }
        ]
      },
    )

  async def remove(self, datapoint_ids: list[str]) -> None:
    if datapoint_ids:
      await self._post(f"{self._api}/{self._index}:removeDatapoints", {"datapointIds": datapoint_ids})

  async def query(self, vector: list[float], project_id: str, kinds: Optional[list[str]], limit: int) -> list[tuple[str, float]]:
    restricts = [{"namespace": "project", "allowList": [project_id]}]
    if kinds:
      restricts.append({"namespace": "kind", "allowList": list(kinds)})
    body = await self._post(
      f"{self._query_api}/{self._endpoint}:findNeighbors",
      {
        "deployedIndexId": self._deployed_index_id,
        "queries": [{"datapoint": {"datapointId": "query", "featureVector": vector, "restricts": restricts}, "neighborCount": limit}],
      },
    )
    neighbors = (body.get("nearestNeighbors") or [{}])[0].get("neighbors") or []
    return [(n["datapoint"]["datapointId"], float(n.get("distance", 0.0))) for n in neighbors]
