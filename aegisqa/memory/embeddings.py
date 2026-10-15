"""Text embeddings for the semantic store (Gemini embedding model on Vertex AI).

When embeddings aren't available (no credentials, model not enabled), the
memory layer keeps working on keyword search alone: `embed` returns None and
retries no more than every few minutes, so a missing credential doesn't slow
every write down.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, Protocol

from ..config import Settings
from ..db.models import EMBEDDING_DIMENSIONS

logger = logging.getLogger(__name__)

RETRY_AFTER_FAILURE_SECONDS = 300


class Embedder(Protocol):
  async def embed(self, text: str, *, query: bool = False) -> Optional[list[float]]: ...


class VertexEmbedder:
  def __init__(self, settings: Settings):
    self._settings = settings
    self._client = None
    self._disabled_until = 0.0
    self._lock = asyncio.Lock()

  def _get_client(self):  # type: ignore[no-untyped-def]
    if self._client is None:
      from google import genai

      kwargs = {}
      if self._settings.embedding_location:
        kwargs["location"] = self._settings.embedding_location
      self._client = genai.Client(**kwargs)
    return self._client

  async def embed(self, text: str, *, query: bool = False) -> Optional[list[float]]:
    if time.monotonic() < self._disabled_until or not text.strip():
      return None
    from google.genai import types

    try:
      response = await self._get_client().aio.models.embed_content(
        model=self._settings.embedding_model,
        contents=text[:8000],
        config=types.EmbedContentConfig(
          output_dimensionality=EMBEDDING_DIMENSIONS,
          task_type="RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT",
        ),
      )
      values = list(response.embeddings[0].values or [])
    except Exception as err:  # noqa: BLE001 -- memory degrades to keyword search
      async with self._lock:
        if time.monotonic() >= self._disabled_until:
          logger.warning(
            "embeddings unavailable (%s) -- semantic memory search is off; retrying in %ss",
            err,
            RETRY_AFTER_FAILURE_SECONDS,
          )
          self._disabled_until = time.monotonic() + RETRY_AFTER_FAILURE_SECONDS
      return None
    if len(values) != EMBEDDING_DIMENSIONS:
      logger.warning("embedding model returned %s dimensions, expected %s", len(values), EMBEDDING_DIMENSIONS)
      return None
    return values


class NoEmbedder:
  async def embed(self, text: str, *, query: bool = False) -> Optional[list[float]]:
    return None
