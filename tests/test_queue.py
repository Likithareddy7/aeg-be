"""Queued execution (TASK_QUEUE=pubsub) without Pub/Sub: a loopback publisher
delivers run messages to the real worker code, whose progress events come
back through the real /internal/events endpoint. Internal-call auth is
checked throughout."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Iterator, Optional

import httpx
import pytest

from .fakes import FakeBrowsers
from .harness import RunningApp, requires_test_database, sql
from .scripted import AppModel

pytestmark = requires_test_database

REUSED_ID = "existing-wrong-password-test"

TOKENS = {"worker-token": "worker@test.iam", "intruder-token": "someone@else.iam"}


async def verifier(token: str) -> Optional[str]:
  return TOKENS.get(token)


class LoopbackPublisher:
  """Stands in for Pub/Sub. Runs on the app's event loop (publish is called there)."""

  def __init__(self) -> None:
    self.messages: list[tuple[str, dict[str, Any]]] = []
    self.base_url = ""
    self.forwarder: Any = None
    self.loop: Optional[asyncio.AbstractEventLoop] = None

  async def publish(self, topic: str, payload: dict[str, Any]) -> None:
    self.messages.append((topic, payload))
    self.loop = asyncio.get_running_loop()
    self.loop.create_task(self._deliver(topic, payload))

  def redeliver(self, topic: str, payload: dict[str, Any]) -> None:
    """Delivers a message again (as Pub/Sub may), from the test thread."""
    assert self.loop is not None
    asyncio.run_coroutine_threadsafe(self._deliver(topic, payload), self.loop).result(timeout=30)

  async def _deliver(self, topic: str, payload: dict[str, Any]) -> None:
    await asyncio.sleep(0.05)
    from aegisqa import worker

    if self.forwarder is None:

      async def token() -> str:
        return "worker-token"

      self.forwarder = worker.EventForwarder(self.base_url, token)
    await worker.handle(payload, self.forwarder)


@pytest.fixture(scope="module")
def env() -> Iterator[dict[str, Any]]:
  publisher = LoopbackPublisher()
  model = AppModel(model="scripted-queue-model")
  app = RunningApp(model, FakeBrowsers(), publisher=publisher, verifier=verifier)
  publisher.base_url = app.base
  admin = app.client()
  project = admin.post("/api/projects", json={"name": "Queued"}).json()
  admin.post(f"/api/projects/{project['id']}/urls", json={"url": "https://app.test/login"})
  sql(
    'INSERT INTO "TestCase" (id, "projectId", title, objective, preconditions, "testData", "expectedResult", priority, type, status, tags, version, "updatedAt") '
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())",
    (REUSED_ID, project["id"], "Wrong password shows an error", "Sign in with a wrong password", "Logged out", "{}",
     "An error message is shown", "high", "negative", "approved", "[]", 1),
  )
  sql(
    'INSERT INTO "TestStep" (id, "testCaseId", "order", action, selector, description, enabled) VALUES (%s,%s,%s,%s,%s,%s,%s)',
    ("existing-step", REUSED_ID, 1, "click", json.dumps({"strategy": "role", "role": "button", "name": "Sign in", "description": "Sign in button"}),
     "Submit wrong password", True),
  )
  yield {"app": app, "admin": admin, "project": project, "publisher": publisher, "model": model}
  app.stop()


def _wait_for(fetch: Any, done: Any, timeout: float = 30) -> Any:
  deadline = time.monotonic() + timeout
  while True:
    value = fetch()
    if done(value) or time.monotonic() > deadline:
      return value
    time.sleep(0.2)


def test_internal_endpoints_require_an_allowed_google_identity(env: dict[str, Any]) -> None:
  base = env["app"].base
  with httpx.Client(base_url=base) as client:
    assert client.post("/internal/events", json={}).status_code == 401
    assert client.post("/internal/events", json={}, headers={"Authorization": "Bearer forged"}).status_code == 401
    assert client.post("/internal/events", json={}, headers={"Authorization": "Bearer intruder-token"}).status_code == 403


def test_runs_are_queued_and_executed_by_the_worker(env: dict[str, Any]) -> None:
  import websockets

  admin, publisher, pid = env["admin"], env["publisher"], env["project"]["id"]
  cookie = "; ".join(f"{k}={v}" for k, v in admin.cookies.items())

  async def run_and_watch() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    url = env["app"].base.replace("http", "ws", 1) + f"/ws?projectId={pid}"
    async with websockets.connect(url, additional_headers={"Cookie": cookie}) as ws:
      await asyncio.sleep(0.3)
      async with httpx.AsyncClient(base_url=env["app"].base, cookies=admin.cookies, timeout=30) as client:
        queued = (await client.post(f"/api/testcases/{REUSED_ID}/run", json={})).json()
      events = []
      while True:
        event = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
        events.append(event)
        if event["type"] == "run.completed":
          return queued, events

  queued, events = asyncio.run(run_and_watch())
  # The API answered straight away with a pending run...
  assert queued["status"] == "pending" and queued["results"] == []
  topic, payload = publisher.messages[-1]
  assert topic == "projects/test/topics/runs" and payload["testRunId"] == queued["id"] and payload["triggeredBy"] == "user"
  # ...and the worker's live progress reached the browser through /internal/events.
  assert [(e["stepOrder"], e["status"]) for e in events if e["type"] == "run.progress"] == [(1, "running"), (1, "passed")]
  completed = next(e for e in events if e["type"] == "run.completed")["testRun"]
  assert completed["id"] == queued["id"] and completed["status"] == "passed"
  assert admin.get(f"/api/testruns/{queued['id']}").json()["status"] == "passed"

  # Run all: every run queued at once, ids returned immediately.
  batch = admin.post(f"/api/projects/{pid}/testcases/run-all", json={}).json()
  assert len(batch["testRunIds"]) == 1
  final = _wait_for(lambda: admin.get(f"/api/testruns/{batch['testRunIds'][0]}").json(), lambda r: r["status"] == "passed")
  assert final["batchId"] == batch["batchId"]

  # A redelivered message for a run that already finished is skipped, not re-run.
  before = admin.get(f"/api/testruns/{queued['id']}").json()
  publisher.redeliver(topic, payload)
  after = admin.get(f"/api/testruns/{queued['id']}").json()
  assert after["status"] == "passed" and after["finishedAt"] == before["finishedAt"]

