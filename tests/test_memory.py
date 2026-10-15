"""The learning loop and hybrid search against a real PostgreSQL + pgvector:
things that happen in the app show up in memory and can be found again."""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Iterator

import httpx
import pytest

from .fakes import FakeBrowsers, call_response, text_response
from .harness import RunningApp, requires_test_database, sql
from .scripted import AppModel, make_test_case

pytestmark = requires_test_database


def _eventually(fetch: Callable[[], Any], check: Callable[[Any], bool], timeout: float = 20) -> Any:
  deadline = time.monotonic() + timeout
  while True:
    value = fetch()
    if check(value) or time.monotonic() > deadline:
      return value
    time.sleep(0.2)


@pytest.fixture(scope="module")
def env() -> Iterator[dict[str, Any]]:
  model = AppModel(model="scripted-memory-model")
  app = RunningApp(model, FakeBrowsers())
  admin = app.client()
  project = admin.post("/api/projects", json={"name": "Memory"}).json()
  admin.post(f"/api/projects/{project['id']}/urls", json={"url": "https://app.test/login"})
  yield {"app": app, "model": model, "admin": admin, "project": project}
  app.stop()


def test_learning_loop_and_hybrid_search(env: dict[str, Any]) -> None:
  model, admin, pid = env["model"], env["admin"], env["project"]["id"]
  memory = lambda **params: admin.get(f"/api/projects/{pid}/memory", params=params).json()  # noqa: E731

  # 1. The agent saves a test case through chat -> remembered, with an embedding.
  model.script[:] = [
    call_response(
      "propose_test_cases",
      {"testCases": [make_test_case("Checkout applies a coupon code", "Sign in"), make_test_case("Profile photo upload", "Delete account")]},
    ),
    text_response("Saved."),
  ]
  import asyncio

  import websockets

  cookie = "; ".join(f"{k}={v}" for k, v in admin.cookies.items())

  async def chat() -> None:
    url = env["app"].base.replace("http", "ws", 1) + f"/ws?projectId={pid}"
    async with websockets.connect(url, additional_headers={"Cookie": cookie}) as ws:
      await ws.send(json.dumps({"type": "chat.send", "text": "write tests"}))
      while True:
        event = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
        if event["type"] == "agent.activity" and event["event"]["stage"] in ("completed", "failed", "waiting_for_user"):
          return

  asyncio.run(chat())
  records = _eventually(lambda: memory(kind="test_case"), lambda r: len(r) == 2)
  assert {r["title"] for r in records} == {"Checkout applies a coupon code", "Profile photo upload"}
  assert all(r["module"] == "Authentication" and r["metadata"]["status"] == "draft" for r in records)
  assert sql('SELECT count(*) FROM "MemoryRecord" WHERE embedding IS NOT NULL')[0][0] == 2

  # 2. Hybrid search: the right test comes first, found by both keyword and meaning.
  hits = memory(q="coupon checkout")
  assert hits[0]["title"] == "Checkout applies a coupon code"
  assert set(hits[0]["matchedBy"]) == {"keyword", "semantic"}

  # 3. Approving re-indexes (status changes, no new embedding needed).
  coupon_id = next(r["refId"] for r in records if r["title"].startswith("Checkout"))
  admin.post("/api/users", json={"username": "rev", "password": "rev-pass1", "role": "member"})
  admin.post(f"/api/projects/{pid}/members", json={"username": "rev", "role": "reviewer"})
  reviewer = httpx.Client(base_url=env["app"].base)
  reviewer.post("/api/auth/login", json={"username": "rev", "password": "rev-pass1"})
  assert reviewer.post(f"/api/testcases/{coupon_id}/review", json={"decision": "approve"}).status_code == 200
  _eventually(lambda: memory(kind="test_case"), lambda r: any(x["metadata"]["status"] == "approved" for x in r))

  # 4. A failing run is remembered with an error code you can look up exactly.
  photo_id = next(r["refId"] for r in records if r["title"].startswith("Profile"))
  run = admin.post(f"/api/testcases/{photo_id}/run", json={}).json()
  assert run["status"] == "failed"
  failures = _eventually(lambda: memory(kind="run_result", errorCode="ELEMENT_NOT_FOUND"), lambda r: len(r) == 1)
  assert failures[0]["refId"] == run["id"] and "Remove the step." in failures[0]["snippet"]
  assert memory(q="profile photo", kind="run_result")[0]["refId"] == run["id"]

  # 5. Deleting a test case removes it from memory.
  assert admin.delete(f"/api/testcases/{photo_id}").status_code == 204
  _eventually(lambda: memory(kind="test_case"), lambda r: len(r) == 1)
  assert [r["refId"] for r in memory(kind="test_case")] == [coupon_id]

  assert admin.get(f"/api/projects/{pid}/memory", params={"kind": "nonsense"}).status_code == 400
