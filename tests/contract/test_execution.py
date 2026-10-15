"""Real test runs: drive a real browser through the backend. No LLM needed --
a passing run never calls one, and a failing step falls back to a generic
suggested fix when no model is reachable."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx
import pytest

from .conftest import BASE_URL, Db, FakeAzureDevOps, Users, default_steps, seed_test_case, selector

pytestmark = pytest.mark.browser

RUN_KEYS = {
  "id", "testCaseId", "environmentId", "batchId", "status", "triggeredBy", "startedAt", "finishedAt", "tracePath",
  "videoPath", "continuedFromChat", "resumedFromRunId", "resumedFromStepOrder", "inheritedResults", "dataRowIndex",
  "results", "logs", "issuePushes",
}
RESULT_KEYS = {
  "id", "testRunId", "testStepId", "stepOrder", "action", "status", "actualResult", "errorMessage", "suggestedFix",
  "recovered", "durationMs", "pageUrl", "screenshots",
}


def _passing_case(db: Db, project_id: str, user_id: str, site: str, **kwargs: Any) -> str:
  steps = default_steps(site)
  steps[1]["value"] = "a@b.test"
  return seed_test_case(db, project_id, user_id, steps=steps, **kwargs)


def _failing_case(db: Db, project_id: str, user_id: str, site: str) -> str:
  steps = [
    {"order": 1, "action": "navigate", "value": f"{site}/login.html", "description": "Open login", "selector": None},
    {"order": 2, "action": "click", "value": None, "description": "Delete account", "selector": selector("button", "Delete account")},
    {"order": 3, "action": "assertText", "value": "never", "description": "Unreached", "selector": None},
  ]
  return seed_test_case(db, project_id, user_id, steps=steps, title="Broken on purpose")


def test_passing_run(admin: httpx.Client, project: dict, db: Db, admin_user: dict, site: str) -> None:
  tc_id = _passing_case(db, project["id"], admin_user["id"], site)
  response = admin.post(f"/api/testcases/{tc_id}/run", json={})
  assert response.status_code == 201, response.text
  run = response.json()
  assert set(run) == RUN_KEYS
  assert run["status"] == "passed" and run["triggeredBy"] == "user" and run["finishedAt"]
  assert [r["status"] for r in run["results"]] == ["passed"] * 4
  assert set(run["results"][0]) == RESULT_KEYS
  assert run["results"][0]["actualResult"] == f"Navigated to {site}/login.html"
  assert run["results"][1]["actualResult"] == 'Filled Email textbox with "a@b.test"'
  assert run["results"][2]["actualResult"] == "Clicked Sign in button"
  assert run["results"][3]["actualResult"] == 'Verified page contains text "Welcome back"'
  assert run["results"][2]["pageUrl"] == f"{site}/login.html"
  assert all(len(r["screenshots"]) == 1 for r in run["results"])
  shot = run["results"][0]["screenshots"][0]["filePath"]
  assert httpx.get(f"{BASE_URL}/screenshots/{shot}").status_code == 200
  messages = [log["message"] for log in run["logs"] if log["category"] == "execution"]
  assert messages[0].startswith("Starting run for") and messages[-1] == "Run finished: passed."
  assert "storageStateJson" not in response.text

  assert admin.get(f"/api/testruns/{run['id']}").json()["id"] == run["id"]
  assert [r["id"] for r in admin.get(f"/api/testruns/by-testcase/{tc_id}").json()] == [run["id"]]
  trace = admin.get(f"/api/testruns/{run['id']}/trace").json()
  assert set(trace) == {"testRun", "consoleLogs", "networkLogs"}
  assert all(log["category"] == "network" for log in trace["networkLogs"])
  assert admin.get("/api/testruns/missing").status_code == 404


def test_failing_run_and_rerun(admin: httpx.Client, project: dict, db: Db, admin_user: dict, site: str) -> None:
  pid = project["id"]
  tc_id = _failing_case(db, pid, admin_user["id"], site)
  run = admin.post(f"/api/testcases/{tc_id}/run", json={}).json()
  assert run["status"] == "failed"
  assert [r["status"] for r in run["results"]] == ["passed", "failed"]  # stops at the first failure
  failed = run["results"][1]
  assert failed["errorMessage"] and failed["suggestedFix"]
  assert len(failed["screenshots"]) == 1

  failing = admin.get(f"/api/projects/{pid}/failing-tests").json()
  assert [f["id"] for f in failing] == [tc_id]
  assert failing[0]["latestRun"]["id"] == run["id"] and failing[0]["latestRun"]["status"] == "failed"
  dash = admin.get(f"/api/projects/{pid}/dashboard").json()
  assert dash["failCount"] == 1 and dash["failingTests"][0]["id"] == tc_id
  assert dash["recentRuns"][0]["testCaseTitle"] == "Broken on purpose"

  rerun = admin.post(f"/api/testruns/{run['id']}/rerun", json={})
  assert rerun.status_code == 201 and rerun.json()["triggeredBy"] == "rerun"

  resumed = admin.post(f"/api/testruns/{run['id']}/rerun", json={"resumeFromStepOrder": 2}).json()
  assert resumed["resumedFromRunId"] == run["id"] and resumed["resumedFromStepOrder"] == 2
  assert [r["stepOrder"] for r in resumed["results"]] == [2]
  fetched = admin.get(f"/api/testruns/{resumed['id']}").json()
  assert [r["stepOrder"] for r in fetched["inheritedResults"]] == [1]
  assert admin.post("/api/testruns/missing/rerun", json={}).status_code == 404


def test_run_all_reports_and_data_driven_runs(admin: httpx.Client, project: dict, db: Db, admin_user: dict, site: str) -> None:
  pid = project["id"]
  passing = _passing_case(db, pid, admin_user["id"], site, status="approved", title="Batch pass")
  _passing_case(db, pid, admin_user["id"], site, status="draft", title="Not approved")

  batch = admin.post(f"/api/projects/{pid}/testcases/run-all", json={})
  assert batch.status_code == 202
  body = batch.json()
  assert set(body) == {"batchId", "testRunIds"} and len(body["testRunIds"]) == 1
  assert body["batchId"].startswith("batch_")

  junit = admin.get(f"/api/testruns/batch/{body['batchId']}/junit")
  assert junit.headers["content-type"].startswith("application/xml")
  assert junit.text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
  assert re.search(r'<testsuite name="batch_[^"]+" tests="1" failures="0" errors="0" skipped="0" time="[\d.]+">', junit.text)
  assert '<testcase classname="' in junit.text and 'name="Batch pass"' in junit.text
  html = admin.get(f"/api/testruns/batch/{body['batchId']}/html")
  assert html.headers["content-type"].startswith("text/html") and "<td>Batch pass</td>" in html.text
  assert admin.get("/api/testruns/batch/nope/junit").status_code == 404

  steps = default_steps(site)  # step 2 fills {{data.email}}
  data_tc = seed_test_case(db, pid, admin_user["id"], steps=steps, title="Data driven")
  admin.put(
    f"/api/testcases/{data_tc}/data-set",
    json={"text": "email\nfirst@x.test\nsecond@x.test\n", "format": "csv", "secretColumns": ["email"]},
  )
  data_runs = admin.post(f"/api/testcases/{data_tc}/run-with-data", json={})
  assert data_runs.status_code == 202
  run_ids = data_runs.json()["testRunIds"]
  assert len(run_ids) == 2
  runs = [admin.get(f"/api/testruns/{rid}").json() for rid in run_ids]
  assert sorted(r["dataRowIndex"] for r in runs) == [0, 1]
  assert all(r["status"] == "passed" for r in runs)
  # Secret data columns are masked in stored results.
  assert all(r["results"][1]["actualResult"] == 'Filled Email textbox with "••••••"' for r in runs)
  assert "first@x.test" not in json.dumps(runs)

  no_data = admin.post(f"/api/testcases/{passing}/run-with-data", json={})
  assert no_data.status_code == 500


def test_push_failed_run_to_azure_devops(admin: httpx.Client, project: dict, db: Db, admin_user: dict, site: str, fake_ado: FakeAzureDevOps) -> None:
  pid = project["id"]
  run = admin.post(f"/api/testcases/{_failing_case(db, pid, admin_user['id'], site)}/run", json={}).json()

  assert admin.post(f"/api/testruns/{run['id']}/push-to/azureDevOps").status_code == 400  # not configured
  assert admin.post(f"/api/testruns/{run['id']}/push-to/jira").status_code == 400
  admin.put(
    f"/api/projects/{pid}/integrations/azureDevOps",
    json={"baseUrl": f"{fake_ado.url}/acme", "email": "", "projectKey": "QA Project", "apiToken": "pat-xyz"},
  )
  pushed = admin.post(f"/api/testruns/{run['id']}/push-to/azureDevOps")
  assert pushed.status_code == 201
  record = pushed.json()
  assert set(record) == {"id", "testRunId", "type", "status", "issueKey", "issueUrl", "errorMessage", "createdAt", "pushedAt"}
  assert record["status"] == "pushed" and record["issueKey"] == "#4242"
  assert record["issueUrl"] == f"{fake_ado.url}/acme/QA%20Project/_workitems/edit/4242"

  [request] = fake_ado.requests
  assert request["path"] == "/acme/QA%20Project/_apis/wit/workitems/$Bug?api-version=7.1"
  assert request["auth"] == "Basic OnBhdC14eXo="  # base64(":pat-xyz")
  assert request["contentType"] == "application/json-patch+json"
  assert request["body"][0] == {"op": "add", "path": "/fields/System.Title", "value": f"Broken on purpose — failed run {run['id'][-8:]}"}
  assert "Step: Delete account" in request["body"][1]["value"] and "<br>" in request["body"][1]["value"]

  # Idempotent: a repeat push returns the same issue without calling Azure DevOps again.
  repeat = admin.post(f"/api/testruns/{run['id']}/push-to/azureDevOps")
  assert repeat.status_code == 200 and repeat.json()["id"] == record["id"] and len(fake_ado.requests) == 1
  assert admin.get(f"/api/testruns/{run['id']}").json()["issuePushes"][0]["issueKey"] == "#4242"


def test_websocket_auth_and_run_progress(admin: httpx.Client, users: Users, project: dict, db: Db, admin_user: dict, site: str) -> None:
  import websockets

  ws_url = BASE_URL.replace("http", "ws", 1)
  cookie = "; ".join(f"{k}={v}" for k, v in admin.cookies.items())
  tc_id = _passing_case(db, project["id"], admin_user["id"], site)

  async def scenario() -> tuple[int, int, list[dict[str, Any]]]:
    async with websockets.connect(f"{ws_url}/ws?projectId={project['id']}") as anon:
      try:
        await anon.recv()
      except websockets.ConnectionClosed as closed:
        anon_code = closed.rcvd.code if closed.rcvd else -1
    async with websockets.connect(f"{ws_url}/ws", additional_headers={"Cookie": cookie}) as no_project:
      try:
        await no_project.recv()
      except websockets.ConnectionClosed as closed:
        no_project_code = closed.rcvd.code if closed.rcvd else -1

    events: list[dict[str, Any]] = []
    async with websockets.connect(f"{ws_url}/ws?projectId={project['id']}", additional_headers={"Cookie": cookie}) as ws:
      await asyncio.sleep(0.5)  # the Node backend authenticates before it starts listening
      await ws.send(json.dumps({"type": "run.start", "testCaseId": tc_id}))
      while True:
        event = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
        events.append(event)
        if event["type"] == "run.completed":
          break
      await ws.send("not json")
      events.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=10)))
    return anon_code, no_project_code, events

  anon_code, no_project_code, events = asyncio.run(scenario())
  assert anon_code == 4001 and no_project_code == 4000
  progress = [(e["stepOrder"], e["status"]) for e in events if e["type"] == "run.progress"]
  assert progress == [(1, "running"), (1, "passed"), (2, "running"), (2, "passed"), (3, "running"), (3, "passed"), (4, "running"), (4, "passed")]
  completed = next(e for e in events if e["type"] == "run.completed")
  assert completed["testRun"]["status"] == "passed"
  assert events[-1] == {"type": "error", "message": "Received malformed message."}
