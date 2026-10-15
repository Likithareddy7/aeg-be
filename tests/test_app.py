"""The whole app in-process: real HTTP + WebSocket server, real PostgreSQL,
a scripted model and a fake browser. Covers what the black-box contract
suite can't without Gemini: chat turns end to end."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Iterator

import httpx
import pytest

from .fakes import FakeBrowsers, ScriptedLlm, call_response, text_response
from .harness import ADMIN_PASSWORD, RunningApp, requires_test_database

pytestmark = requires_test_database


PROPOSED = {
  "title": "Sign in shows welcome",
  "objective": "Valid sign in",
  "preconditions": "Logged out",
  "testData": {"email": "a@b.test"},
  "expectedResult": "Welcome shown",
  "priority": "high",
  "type": "smoke",
  "module": "Authentication",
  "steps": [
    {"order": 1, "action": "navigate", "value": "https://app.test/login", "description": "Open login"},
    {
      "order": 2,
      "action": "click",
      "selector": {"strategy": "role", "role": "button", "name": "Sign in", "description": "Sign in button", "css": None},
      "description": "Submit",
    },
  ],
}


class AppModel(ScriptedLlm):
  """Routes each request by agent: one-shot agents get canned JSON, chat
  turns follow `chat_script`; a user message containing "slow" blocks, and
  "boom" raises."""

  chat_script: list[Any] = []

  async def generate_content_async(self, llm_request: Any, stream: bool = False):  # type: ignore[override]
    self._requests.append(llm_request)
    system = llm_request.config.system_instruction or ""
    if "single structured Playwright test step" in system:
      yield text_response(json.dumps({"action": "click", "selector": {"strategy": "role", "role": "button", "name": "Sign in", "description": "Sign in button"}, "value": None, "description": "Click Sign in"}))
      return
    last = llm_request.contents[-1].parts[0].text or ""
    if "slow" in last:
      await asyncio.sleep(60)
    if "boom" in last:
      raise RuntimeError("Vertex AI quota exceeded (simulated)")
    yield self.chat_script.pop(0)


@pytest.fixture(scope="module")
def server() -> Iterator[dict[str, Any]]:
  model = AppModel(model="scripted-app-model")
  browsers = FakeBrowsers()
  app = RunningApp(model, browsers)
  yield {"base": app.base, "model": model, "browsers": browsers}
  app.stop()


@pytest.fixture(scope="module")
def admin(server: dict[str, Any]) -> Iterator[httpx.Client]:
  client = httpx.Client(base_url=server["base"], timeout=60)
  assert client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}).status_code == 200
  yield client
  client.close()


@pytest.fixture(scope="module")
def project(admin: httpx.Client) -> dict[str, Any]:
  project = admin.post("/api/projects", json={"name": "Chat project"}).json()
  admin.post(f"/api/projects/{project['id']}/urls", json={"url": "https://app.test/login"})
  return project


def _chat(server: dict[str, Any], admin: httpx.Client, project_id: str, text: str, cancel_after: float | None = None) -> list[dict[str, Any]]:
  import websockets

  cookie = "; ".join(f"{k}={v}" for k, v in admin.cookies.items())
  url = server["base"].replace("http", "ws", 1) + f"/ws?projectId={project_id}"

  async def run() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    async with websockets.connect(url, additional_headers={"Cookie": cookie}) as ws:
      await ws.send(json.dumps({"type": "chat.send", "text": text}))
      while True:
        event = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
        events.append(event)
        if cancel_after is not None and event["type"] == "agent.activity" and event["event"]["stage"] == "understanding":
          await asyncio.sleep(cancel_after)
          await ws.send(json.dumps({"type": "chat.cancel", "turnId": event["event"]["turnId"]}))
        if event["type"] == "agent.activity" and event["event"]["stage"] in ("completed", "waiting_for_user", "failed", "cancelled"):
          # A failed turn's error message follows its final status.
          try:
            while True:
              events.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=0.5)))
          except asyncio.TimeoutError:
            return events

  return asyncio.run(run())


def test_chat_turn_saves_test_cases_and_records_everything(server: dict[str, Any], admin: httpx.Client, project: dict[str, Any]) -> None:
  server["model"].chat_script[:] = [
    call_response("browser_navigate", {"url": "https://app.test/login"}, text="Opening the page. "),
    call_response("propose_test_cases", {"testCases": [PROPOSED]}),
    text_response("Saved 1 test case."),
  ]
  pid = project["id"]
  events = _chat(server, admin, pid, "Generate tests for login")

  stages = [e["event"]["stage"] for e in events if e["type"] == "agent.activity"]
  assert stages == ["queued", "understanding", "analyzing", "generating_test_cases", "completed"]
  proposed = next(e for e in events if e["type"] == "testcases.proposed")["testCases"]
  assert [tc["title"] for tc in proposed] == ["Sign in shows welcome"]
  assert proposed[0]["sourcePrompt"] == "Generate tests for login"
  assert proposed[0]["createdBy"]["username"] == "admin"
  assert proposed[0]["steps"][1]["selector"] == {"strategy": "role", "role": "button", "name": "Sign in", "description": "Sign in button"}

  assert [m["role"] for m in admin.get(f"/api/projects/{pid}/chat").json()] == ["user", "assistant"]
  assert admin.get(f"/api/projects/{pid}/chat").json()[1]["content"] == "Opening the page. Saved 1 test case."
  assert len(admin.get(f"/api/projects/{pid}/agent-activity").json()) == 5
  usage = admin.get(f"/api/projects/{pid}/usage").json()
  assert usage["inputTokens"] == 300 and usage["outputTokens"] == 60
  assert [tc["id"] for tc in admin.get(f"/api/projects/{pid}/testcases").json()] == [proposed[0]["id"]]


def test_agent_can_edit_its_test_case_and_rejects_bad_payloads(server: dict[str, Any], admin: httpx.Client, project: dict[str, Any]) -> None:
  pid = project["id"]
  tc_id = admin.get(f"/api/projects/{pid}/testcases").json()[0]["id"]
  server["model"].chat_script[:] = [
    call_response("propose_test_cases", {"testCases": [{"title": "missing everything"}]}),
    call_response("update_test_case", {"testCaseId": tc_id, "updates": {"priority": "critical"}, "summary": "Raised priority"}),
    text_response("Done."),
  ]
  events = _chat(server, admin, pid, "make it critical")
  assert next(e for e in events if e["type"] == "testcases.updated")["testCase"]["priority"] == "critical"
  # The invalid proposal reached the model as a tool error, not a crash.
  requests = server["model"].requests
  errors = [p.function_response.response for c in requests[-1].contents for p in c.parts or [] if p.function_response]
  assert errors[0]["isError"] is True and "Failed to apply propose_test_cases" in errors[0]["content"]
  assert admin.get(f"/api/testcases/{tc_id}").json()["version"] == 2


def test_cancel_and_model_errors(server: dict[str, Any], admin: httpx.Client, project: dict[str, Any]) -> None:
  pid = project["id"]
  cancelled = _chat(server, admin, pid, "please be slow", cancel_after=0.5)
  assert [e["event"]["stage"] for e in cancelled if e["type"] == "agent.activity"][-1] == "cancelled"

  failed = _chat(server, admin, pid, "boom")
  last = [e for e in failed if e["type"] == "agent.activity"][-1]["event"]
  assert last["stage"] == "failed" and "quota exceeded" in last["detail"]
  assert any(e["type"] == "error" for e in failed)


def test_generate_step_uses_a_fresh_browser(server: dict[str, Any], admin: httpx.Client, project: dict[str, Any]) -> None:
  tc_id = admin.get(f"/api/projects/{project['id']}/testcases").json()[0]["id"]
  step = admin.post(f"/api/testcases/{tc_id}/steps/generate", json={"instruction": "click sign in"}).json()
  assert step["action"] == "click" and step["selector"]["name"] == "Sign in" and step["order"] == 0
  browsers = server["browsers"]
  assert any(key.startswith("stepgen:") for key in browsers.closed)


def test_websocket_rejects_non_members(server: dict[str, Any], admin: httpx.Client, project: dict[str, Any]) -> None:
  import websockets

  admin.post("/api/users", json={"username": "outsider", "password": "outsider-pass", "role": "member"})
  outsider = httpx.Client(base_url=server["base"])
  outsider.post("/api/auth/login", json={"username": "outsider", "password": "outsider-pass"})
  cookie = "; ".join(f"{k}={v}" for k, v in outsider.cookies.items())

  async def connect() -> int:
    url = server["base"].replace("http", "ws", 1) + f"/ws?projectId={project['id']}"
    async with websockets.connect(url, additional_headers={"Cookie": cookie}) as ws:
      try:
        await ws.recv()
      except websockets.ConnectionClosed as closed:
        return closed.rcvd.code if closed.rcvd else -1
    return -1

  assert asyncio.run(connect()) == 4003
  outsider.close()
