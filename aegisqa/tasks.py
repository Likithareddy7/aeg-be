"""Background work across processes (TASK_QUEUE=pubsub).

  test runs        publish {"testRunId", "triggeredBy", "options"} to PUBSUB_RUNS_TOPIC
                   -> pull subscription -> `python -m aegisqa.worker` (GKE)
  live progress    the worker POSTs each run event to /internal/events, which
                   forwards it to the browsers watching that project

Every /internal/* call must carry a Google-signed OIDC token for
INTERNAL_AUDIENCE from one of INTERNAL_INVOKERS -- GKE Workload Identity
provides one.

With TASK_QUEUE=inline (local default) none of this is used.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional, Protocol

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from .config import Settings
from .errors import ApiError

logger = logging.getLogger(__name__)


class Publisher(Protocol):
  async def publish(self, topic: str, payload: dict[str, Any]) -> None: ...


class PubSubPublisher:
  def __init__(self, client: Any = None):
    if client is None:
      from google.cloud import pubsub_v1

      client = pubsub_v1.PublisherClient()
    self._client = client

  async def publish(self, topic: str, payload: dict[str, Any]) -> None:
    future = self._client.publish(topic, json.dumps(payload).encode())
    await asyncio.to_thread(future.result, 60)


# ---------------------------------------------------------------------------
# Service-to-service identity
# ---------------------------------------------------------------------------

Verifier = Callable[[str], Awaitable[Optional[str]]]
TokenFetcher = Callable[[], Awaitable[str]]


def google_oidc_verifier(settings: Settings) -> Verifier:
  """Accepts a Google-signed ID token for INTERNAL_AUDIENCE; returns its email."""

  async def verify(token: str) -> Optional[str]:
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    try:
      claims = await asyncio.to_thread(id_token.verify_oauth2_token, token, google_requests.Request(), settings.internal_audience)
    except ValueError:
      return None
    if not claims.get("email_verified", False):
      return None
    return str(claims.get("email", ""))

  return verify


def google_id_token_fetcher(audience: str) -> TokenFetcher:
  """ID tokens for calling the backend, from the metadata server (GKE Workload
  Identity / Cloud Run) or local service-account credentials."""

  async def fetch() -> str:
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    return str(await asyncio.to_thread(id_token.fetch_id_token, google_requests.Request(), audience))

  return fetch


def require_internal_caller(settings: Settings, verifier: Verifier) -> Callable[[Request], Awaitable[str]]:
  async def check(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
      raise ApiError(401, "Missing bearer token")
    email = await verifier(header.split(" ", 1)[1])
    if email is None:
      raise ApiError(401, "Invalid token")
    if email not in settings.internal_invokers:
      raise ApiError(403, f"{email} isn't allowed to call internal endpoints")
    return email

  return check


def internal_router(settings: Settings, verifier: Verifier) -> APIRouter:
  from .ws import registry

  router = APIRouter(prefix="/internal", include_in_schema=False)
  caller = Depends(require_internal_caller(settings, verifier))

  @router.post("/events", dependencies=[caller])
  async def run_event(request: Request) -> Response:
    """A worker's live run progress, forwarded to the project's browsers."""
    body = await request.json()
    registry.broadcast(str(body["projectId"]), body["event"])
    return Response(status_code=204)

  return router
