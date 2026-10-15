"""Test-run worker (TASK_QUEUE=pubsub) -- the "execution engine" on GKE Autopilot.

  python -m aegisqa.worker

Pulls run messages from PUBSUB_RUNS_SUBSCRIPTION, at most MAX_PARALLEL_RUNS at
a time, and executes each one with the same code the backend uses inline
(browser via Playwright MCP, screenshots to the artifact store, self-healing
and failure analysis via Gemini, results to Cloud SQL). Live progress goes to
the backend's /internal/events with an ID token (Workload Identity), so the
browsers watching the project still see it in real time.

A message is acknowledged once its run has finished. Scale it by running more
replicas; Pub/Sub spreads messages across them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from typing import Any, Callable, Optional

import httpx

from . import services
from .agents.browser import BrowserSessionManager
from .config import Settings, settings
from .db import models as m
from .db.base import dispose_engine, session_scope
from .execution import executor
from .memory import build_memory_service
from .storage import build_artifact_store
from .tasks import TokenFetcher, google_id_token_fetcher

logger = logging.getLogger("aegisqa.worker")

Emit = Callable[[dict[str, Any]], None]


class EventForwarder:
  """Sends run events to the backend, in order, without blocking the run."""

  def __init__(self, base_url: str, tokens: TokenFetcher, transport: Optional[httpx.AsyncBaseTransport] = None):
    self._url = f"{base_url}/internal/events"
    self._tokens = tokens
    self._client = httpx.AsyncClient(timeout=10, transport=transport)
    self._queue: asyncio.Queue[Optional[dict[str, Any]]] = asyncio.Queue()
    self._task = asyncio.create_task(self._drain())

  def emitter(self, project_id: str) -> Emit:
    return lambda event: self._queue.put_nowait({"projectId": project_id, "event": event})

  async def _drain(self) -> None:
    while (item := await self._queue.get()) is not None:
      try:
        token = await self._tokens()
        response = await self._client.post(self._url, json=item, headers={"Authorization": f"Bearer {token}"})
        if response.status_code >= 300:
          logger.warning("backend rejected a run event: %s %s", response.status_code, response.text[:200])
      except Exception as err:  # noqa: BLE001 -- progress is best effort; results are in the database
        logger.warning("couldn't forward a run event: %s", err)

  async def close(self) -> None:
    self._queue.put_nowait(None)
    await self._task
    await self._client.aclose()


async def handle(payload: dict[str, Any], forwarder: Optional[EventForwarder]) -> None:
  """Executes one queued run. Already-started runs (a redelivered message)
  are skipped."""
  run_id = str(payload["testRunId"])
  async with session_scope() as db:
    run = await db.get(m.TestRun, run_id)
    if run is None or run.status != "pending":
      logger.info("skipping run %s (status: %s)", run_id, run.status if run else "deleted")
      return
    tc = await db.get(m.TestCase, run.test_case_id)
    if tc is None:
      return
    project_id = tc.project_id
  emit = forwarder.emitter(project_id) if forwarder else None
  await executor.execute_test_case(
    str(payload["testCaseId"]),
    str(payload.get("triggeredBy") or "user"),
    emit,
    executor.options_from_json(payload.get("options") or {}),
    run_id=run_id,
  )


def configure(cfg: Settings) -> None:
  services.configure(
    services.Services(
      model=cfg.gemini_model,
      browsers=BrowserSessionManager(cfg),
      memory=build_memory_service(cfg),
      artifacts=build_artifact_store(cfg),
    )
  )


async def main(cfg: Settings = settings) -> None:
  missing = [
    name
    for name, value in (
      ("PUBSUB_RUNS_SUBSCRIPTION", cfg.pubsub_runs_subscription),
      ("BACKEND_INTERNAL_URL", cfg.backend_internal_url),
      ("INTERNAL_AUDIENCE", cfg.internal_audience),
    )
    if not value
  ]
  if missing:
    raise SystemExit(f"The worker needs {', '.join(missing)}")

  from google.cloud import pubsub_v1

  configure(cfg)
  loop = asyncio.get_running_loop()
  forwarder = EventForwarder(cfg.backend_internal_url, google_id_token_fetcher(cfg.internal_audience))
  stop = asyncio.Event()
  for sig in (signal.SIGINT, signal.SIGTERM):
    loop.add_signal_handler(sig, stop.set)

  def callback(message: Any) -> None:  # runs on a Pub/Sub client thread
    try:
      payload = json.loads(message.data)
    except ValueError:
      logger.error("dropping unreadable message %s", message.message_id)
      message.ack()
      return
    future = asyncio.run_coroutine_threadsafe(handle(payload, forwarder), loop)
    try:
      future.result()
      message.ack()
    except Exception:  # noqa: BLE001 -- nack -> Pub/Sub redelivers (and dead-letters if configured)
      logger.exception("run %s failed to execute", payload.get("testRunId"))
      message.nack()

  subscriber = pubsub_v1.SubscriberClient()
  streaming = subscriber.subscribe(
    cfg.pubsub_runs_subscription,
    callback=callback,
    flow_control=pubsub_v1.types.FlowControl(max_messages=max(1, cfg.max_parallel_runs)),
  )
  logger.info("worker listening on %s (up to %s runs at once)", cfg.pubsub_runs_subscription, cfg.max_parallel_runs)
  await stop.wait()
  logger.info("shutting down")
  streaming.cancel()
  try:
    await asyncio.to_thread(streaming.result, 30)
  except Exception:  # noqa: BLE001 -- cancelled streams end with an error
    pass
  await forwarder.close()
  await services.get().browsers.close_all()
  await dispose_engine()


if __name__ == "__main__":
  logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
  asyncio.run(main())
