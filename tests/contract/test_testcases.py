from __future__ import annotations

import json
import uuid

import httpx
import pytest

from .conftest import (
  Db,
  Users,
  _now,
  add_member,
  selector,
  seed_test_case,
  visual_regression_dir,
)

TEST_CASE_KEYS = {
  "id", "projectId", "urlId", "title", "objective", "preconditions", "testData", "expectedResult", "priority",
  "type", "module", "status", "steps", "createdBy", "openChangeRequestCount", "sourcePrompt", "tags", "version",
  "lastModifiedBy", "createdAt", "updatedAt",
}
STEP_KEYS = {"id", "testCaseId", "order", "action", "selector", "locatorCandidates", "value", "description", "enabled"}


def test_get_test_case_shape(admin: httpx.Client, project: dict, db: Db, admin_user: dict) -> None:
  tc_id = seed_test_case(db, project["id"], admin_user["id"])
  tc = admin.get(f"/api/testcases/{tc_id}").json()
  assert set(tc) == TEST_CASE_KEYS
  assert tc["testData"] == {"email": "a@b.test"} and tc["tags"] == [] and tc["version"] == 1
  assert tc["createdBy"] == {"id": admin_user["id"], "username": admin_user["username"], "role": "admin"}
  assert [s["order"] for s in tc["steps"]] == [1, 2, 3, 4]
  assert set(tc["steps"][0]) == STEP_KEYS
  assert tc["steps"][1]["selector"]["name"] == "Email"
  assert admin.get("/api/testcases/missing").status_code == 404
  assert [t["id"] for t in admin.get(f"/api/projects/{project['id']}/testcases").json()] == [tc_id]


def test_update_test_case(admin: httpx.Client, users: Users, project: dict, db: Db, admin_user: dict) -> None:
  pid = project["id"]
  tc_id = seed_test_case(db, pid, admin_user["id"])
  new_steps = [
    {"order": 1, "action": "navigate", "value": "https://x.test", "description": "Go"},
    {"order": 2, "action": "click", "selector": selector("button", "Go"), "description": "Click go", "enabled": False},
  ]
  response = admin.put(
    f"/api/testcases/{tc_id}",
    json={"title": "Edited", "tags": ["smoke", "login"], "module": None, "priority": "critical", "steps": new_steps, "status": "archived"},
  )
  assert response.status_code == 200
  tc = response.json()
  assert tc["title"] == "Edited" and tc["tags"] == ["smoke", "login"] and tc["module"] is None
  assert tc["priority"] == "critical" and tc["status"] == "archived" and tc["version"] == 2
  assert [(s["order"], s["action"], s["enabled"]) for s in tc["steps"]] == [(1, "navigate", True), (2, "click", False)]
  assert tc["lastModifiedBy"]["id"] == admin_user["id"]

  # Approving through a direct edit is refused; re-sending "approved" when already approved is fine.
  assert admin.put(f"/api/testcases/{tc_id}", json={"status": "approved"}).status_code == 400
  assert admin.put(f"/api/testcases/{tc_id}", json={"priority": "urgent"}).status_code == 400
  assert admin.put("/api/testcases/missing", json={"title": "x"}).status_code == 404

  editor, editor_client = users.create()
  add_member(admin, pid, editor["username"], "editor")
  forbidden = editor_client.put(f"/api/testcases/{tc_id}", json={"title": "Not mine"})
  assert forbidden.status_code == 403  # editors can only edit their own test cases
  viewer, viewer_client = users.create()
  add_member(admin, pid, viewer["username"], "viewer")
  assert viewer_client.put(f"/api/testcases/{tc_id}", json={"title": "x"}).status_code == 403


def test_review_flow(admin: httpx.Client, users: Users, project: dict, db: Db, admin_user: dict) -> None:
  pid = project["id"]
  author, author_client = users.create()
  add_member(admin, pid, author["username"], "editor")
  reviewer, reviewer_client = users.create()
  add_member(admin, pid, reviewer["username"], "reviewer")
  tc_id = seed_test_case(db, pid, author["id"])

  assert author_client.post(f"/api/testcases/{tc_id}/review", json={"decision": "approve"}).status_code == 403
  assert reviewer_client.post(f"/api/testcases/{tc_id}/review", json={"decision": "maybe"}).status_code == 400
  approved = reviewer_client.post(f"/api/testcases/{tc_id}/review", json={"decision": "approve", "note": "LGTM"})
  assert approved.status_code == 200 and approved.json()["status"] == "approved"
  rejected = reviewer_client.post(f"/api/testcases/{tc_id}/review", json={"decision": "reject"})
  assert rejected.json()["status"] == "draft"

  own = seed_test_case(db, pid, reviewer["id"])
  assert reviewer_client.post(f"/api/testcases/{own}/review", json={"decision": "approve"}).status_code == 403
  actions = [e["action"] for e in admin.get(f"/api/projects/{pid}/audit-log").json()]
  assert "testcase.approved" in actions and "testcase.rejected" in actions


def test_delete_test_case(admin: httpx.Client, users: Users, project: dict, db: Db, admin_user: dict) -> None:
  pid = project["id"]
  editor, editor_client = users.create()
  add_member(admin, pid, editor["username"], "editor")
  others = seed_test_case(db, pid, admin_user["id"])
  mine = seed_test_case(db, pid, editor["id"])
  assert editor_client.delete(f"/api/testcases/{others}").status_code == 403
  assert editor_client.delete(f"/api/testcases/{mine}").status_code == 204
  assert admin.get(f"/api/testcases/{mine}").status_code == 404
  assert admin.delete("/api/testcases/missing").status_code == 204
  assert "testcase.deleted" in [e["action"] for e in admin.get(f"/api/projects/{pid}/audit-log").json()]


def test_export_single_test_case(admin: httpx.Client, project: dict, db: Db, admin_user: dict) -> None:
  tc_id = seed_test_case(db, project["id"], admin_user["id"], title="Login works")
  response = admin.get(f"/api/testcases/{tc_id}/export")
  assert response.status_code == 200
  assert response.headers["content-type"].startswith("text/plain")
  assert 'filename="login-works.spec.ts"' in response.headers["content-disposition"]
  script = response.text
  assert script.startswith("import { test, expect } from '@playwright/test';")
  assert "await page.getByRole(\"textbox\", { name: \"Email\" }).fill(\"{{data.email}}\");" in script
  assert "await page.getByRole(\"button\", { name: \"Sign in\" }).click();" in script
  assert "toContainText(\"Welcome back\")" in script


def test_export_with_data_set_and_secret_columns(admin: httpx.Client, project: dict, db: Db, admin_user: dict) -> None:
  tc_id = seed_test_case(db, project["id"], admin_user["id"], title="Data login")
  upload = admin.put(
    f"/api/testcases/{tc_id}/data-set",
    json={"text": "email,password\na@b.test,pw1\nc@d.test,pw2\n", "format": "csv", "secretColumns": ["password"]},
  )
  assert upload.status_code == 200
  script = admin.get(f"/api/testcases/{tc_id}/export").text
  assert "const DATA = [" in script
  assert '{ "email": "a@b.test", "password": process.env.DATA_SECRET_0_PASSWORD },' in script
  assert "for (const [rowIndex, row] of DATA.entries()) {" in script
  assert '.fill(row["email"]);' in script
  assert "pw1" not in script


def test_data_sets(admin: httpx.Client, users: Users, project: dict, db: Db, admin_user: dict) -> None:
  pid = project["id"]
  tc_id = seed_test_case(db, pid, admin_user["id"])
  assert admin.get(f"/api/testcases/{tc_id}/data-set").json() is None

  csv = admin.put(
    f"/api/testcases/{tc_id}/data-set",
    json={"text": 'name,quote\n"Smith, J","He said ""hi"""\nLee,x\n', "format": "csv", "secretColumns": ["quote", "ghost"]},
  )
  assert csv.status_code == 200
  ds = csv.json()
  assert set(ds) == {"id", "testCaseId", "columns", "secretColumns", "rows", "source", "createdAt", "updatedAt"}
  assert ds["columns"] == ["name", "quote"] and ds["secretColumns"] == ["quote"]
  assert ds["rows"] == [{"name": "Smith, J", "quote": 'He said "hi"'}, {"name": "Lee", "quote": "x"}]

  flags = admin.put(f"/api/testcases/{tc_id}/data-set", json={"secretColumns": ["name"]})
  assert flags.status_code == 200 and flags.json()["secretColumns"] == ["name"] and len(flags.json()["rows"]) == 2

  js = admin.put(
    f"/api/testcases/{tc_id}/data-set",
    json={"text": json.dumps([{"a": 1, "b": True}, {"c": None, "a": "x"}]), "format": "json"},
  ).json()
  assert js["columns"] == ["a", "b", "c"] and js["source"] == "json"
  assert js["rows"] == [{"a": "1", "b": "true", "c": ""}, {"a": "x", "b": "", "c": ""}]

  for bad in [
    {"text": "only_header\n", "format": "csv"},
    {"text": "{not json", "format": "json"},
    {"text": "[]", "format": "json"},
    {"text": "[1, 2]", "format": "json"},
  ]:
    assert admin.put(f"/api/testcases/{tc_id}/data-set", json=bad).status_code == 400, bad

  assert admin.delete(f"/api/testcases/{tc_id}/data-set").status_code == 204
  assert admin.get(f"/api/testcases/{tc_id}/data-set").json() is None
  missing = admin.put(f"/api/testcases/{tc_id}/data-set", json={"secretColumns": []})
  assert missing.status_code == 404

  viewer, viewer_client = users.create()
  add_member(admin, pid, viewer["username"], "viewer")
  assert viewer_client.get(f"/api/testcases/{tc_id}/data-set").status_code == 200
  assert viewer_client.delete(f"/api/testcases/{tc_id}/data-set").status_code == 403


def test_change_requests(admin: httpx.Client, users: Users, project: dict, db: Db, admin_user: dict) -> None:
  pid = project["id"]
  owner, owner_client = users.create()
  add_member(admin, pid, owner["username"], "editor")
  viewer, viewer_client = users.create()
  add_member(admin, pid, viewer["username"], "viewer")
  tc_id = seed_test_case(db, pid, owner["id"])

  assert viewer_client.post(f"/api/testcases/{tc_id}/change-requests", json={"note": ""}).status_code == 400
  created = viewer_client.post(f"/api/testcases/{tc_id}/change-requests", json={"note": "Step 3 is flaky"})
  assert created.status_code == 201
  cr = created.json()
  assert set(cr) == {"id", "testCaseId", "testCaseTitle", "requestedBy", "note", "status", "createdAt", "resolvedAt"}
  assert cr["status"] == "open" and cr["requestedBy"]["id"] == viewer["id"]

  assert owner_client.get(f"/api/testcases/{tc_id}").json()["openChangeRequestCount"] == 1
  assert [c["id"] for c in owner_client.get(f"/api/testcases/{tc_id}/change-requests").json()] == [cr["id"]]
  dash = admin.get(f"/api/projects/{pid}/dashboard").json()
  assert dash["openChangeRequestCount"] == 1 and dash["openChangeRequests"][0]["id"] == cr["id"]

  assert viewer_client.post(f"/api/change-requests/{cr['id']}/resolve").status_code == 403
  resolved = owner_client.post(f"/api/change-requests/{cr['id']}/resolve")
  assert resolved.status_code == 200 and resolved.json()["status"] == "resolved" and resolved.json()["resolvedAt"]
  assert admin.post("/api/change-requests/missing/resolve").status_code == 404


def test_flows_and_inserting_them_into_test_cases(admin: httpx.Client, users: Users, project: dict, db: Db, admin_user: dict) -> None:
  pid = project["id"]
  steps_v1 = [
    {"order": 1, "action": "navigate", "value": "https://x.test/login", "description": "Open"},
    {"order": 2, "action": "click", "selector": selector("button", "Log in"), "description": "Log in"},
  ]
  created = admin.post(f"/api/projects/{pid}/flows", json={"name": "Login", "description": "std user", "steps": steps_v1})
  assert created.status_code == 201
  flow = created.json()
  assert set(flow) == {"id", "projectId", "name", "description", "createdBy", "versions", "createdAt", "updatedAt"}
  v1 = flow["versions"][0]
  assert v1["version"] == 1 and len(v1["steps"]) == 2
  assert admin.post(f"/api/projects/{pid}/flows", json={"name": "", "steps": []}).status_code == 400

  summary = admin.get(f"/api/projects/{pid}/flows").json()
  assert len(summary) == 1 and summary[0]["latestVersion"] == 1 and summary[0]["latestStepCount"] == 2
  assert set(summary[0]) == {"id", "projectId", "name", "description", "createdBy", "latestVersion", "latestStepCount", "createdAt", "updatedAt"}

  tc_id = seed_test_case(db, pid, admin_user["id"])
  inserted = admin.post(f"/api/testcases/{tc_id}/steps/insert-flow", json={"flowVersionId": v1["id"]})
  assert inserted.status_code == 200
  assert [s["order"] for s in inserted.json()["steps"]] == [1, 2, 3, 4, 5, 6]
  assert admin.post(f"/api/testcases/{tc_id}/steps/insert-flow", json={"flowVersionId": "nope"}).status_code == 400

  tc = admin.get(f"/api/testcases/{tc_id}").json()
  provenance = tc["steps"][4]["sourceFlow"]
  assert provenance == {"flowId": flow["id"], "flowName": "Login", "versionId": v1["id"], "version": 1, "isLatest": True}
  assert "sourceFlow" not in tc["steps"][0] or tc["steps"][0]["sourceFlow"] is None

  steps_v2 = steps_v1 + [{"order": 3, "action": "assertUrl", "value": "/home", "description": "Landed"}]
  v2 = admin.post(f"/api/flows/{flow['id']}/versions", json={"steps": steps_v2, "note": "assert landing"})
  assert v2.status_code == 201 and v2.json()["version"] == 2 and v2.json()["note"] == "assert landing"
  assert admin.get(f"/api/testcases/{tc_id}").json()["steps"][4]["sourceFlow"]["isLatest"] is False

  updated = admin.post(
    f"/api/testcases/{tc_id}/steps/update-flow-block",
    json={"sourceFlowVersionId": v1["id"], "toFlowVersionId": v2.json()["id"]},
  )
  assert updated.status_code == 200
  steps = updated.json()["steps"]
  assert [s["order"] for s in steps] == list(range(1, 8)) and steps[-1]["action"] == "assertUrl"
  again = admin.post(
    f"/api/testcases/{tc_id}/steps/update-flow-block",
    json={"sourceFlowVersionId": v1["id"], "toFlowVersionId": v2.json()["id"]},
  )
  assert again.status_code == 400

  full = admin.get(f"/api/flows/{flow['id']}").json()
  assert [v["version"] for v in full["versions"]] == [1, 2]
  renamed = admin.put(f"/api/flows/{flow['id']}", json={"name": "Login v2", "description": None})
  assert renamed.status_code == 200 and renamed.json()["name"] == "Login v2" and renamed.json()["description"] is None

  editor, editor_client = users.create()
  add_member(admin, pid, editor["username"], "editor")
  assert editor_client.get(f"/api/flows/{flow['id']}").status_code == 200
  assert editor_client.put(f"/api/flows/{flow['id']}", json={"name": "mine"}).status_code == 403
  assert editor_client.delete(f"/api/flows/{flow['id']}").status_code == 403
  assert admin.delete(f"/api/flows/{flow['id']}").status_code == 204
  assert admin.get(f"/api/flows/{flow['id']}").status_code == 404
  # Deleting a flow never breaks the test cases that used it.
  assert admin.get(f"/api/testcases/{tc_id}").status_code == 200


def _seed_run_result(db: Db, tc_id: str) -> tuple[str, str, str]:
  step_id = db.execute('SELECT id FROM "TestStep" WHERE "testCaseId" = %s ORDER BY "order" LIMIT 1 OFFSET 2', (tc_id,))[0][0]
  run_id = "run" + uuid.uuid4().hex[:20]
  result_id = "res" + uuid.uuid4().hex[:20]
  now = _now()
  db.execute(
    'INSERT INTO "TestRun" (id, "testCaseId", status, "triggeredBy", "continuedFromChat", "startedAt", "finishedAt") VALUES (%s,%s,%s,%s,%s,%s,%s)',
    (run_id, tc_id, "passed", "user", False, now, now),
  )
  db.execute(
    'INSERT INTO "TestRunResult" (id, "testRunId", "testStepId", "stepOrder", action, status, recovered, "durationMs") VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
    (result_id, run_id, step_id, 3, "click", "passed", True, 10),
  )
  return step_id, run_id, result_id


def test_healing_events(admin: httpx.Client, users: Users, project: dict, db: Db, admin_user: dict) -> None:
  pid = project["id"]
  tc_id = seed_test_case(db, pid, admin_user["id"])
  step_id, _, result_id = _seed_run_result(db, tc_id)
  new_sel = selector("button", "Log in")
  ids = []
  for _ in range(2):
    event_id = "he" + uuid.uuid4().hex[:20]
    db.execute(
      'INSERT INTO "HealingEvent" (id, "testStepId", "testRunResultId", "oldSelector", "newSelector", confidence, note, approved, dismissed, "createdAt") '
      "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
      (event_id, step_id, result_id, json.dumps(selector("button", "Sign in")), json.dumps(new_sel), "high", "renamed", False, False, _now()),
    )
    ids.append(event_id)

  assert admin.get("/api/healing-events").status_code == 400
  pending = admin.get(f"/api/healing-events?projectId={pid}").json()
  assert {e["id"] for e in pending} == set(ids)
  assert set(pending[0]) == {
    "id", "testStepId", "testRunResultId", "oldSelector", "newSelector", "confidence", "note", "screenshotPath",
    "approved", "dismissed", "createdAt",
  }

  viewer, viewer_client = users.create()
  add_member(admin, pid, viewer["username"], "viewer")
  assert viewer_client.get(f"/api/healing-events?projectId={pid}").status_code == 200
  assert viewer_client.post(f"/api/healing-events/{ids[0]}/approve", json={}).status_code == 403

  approved = admin.post(f"/api/healing-events/{ids[0]}/approve", json={"makePrimary": True})
  assert approved.status_code == 200 and approved.json()["approved"] is True
  step = next(s for s in admin.get(f"/api/testcases/{tc_id}").json()["steps"] if s["id"] == step_id)
  assert step["selector"]["name"] == "Log in" and step["locatorCandidates"] == [new_sel]

  assert admin.post(f"/api/healing-events/{ids[1]}/dismiss").json()["dismissed"] is True
  assert admin.get(f"/api/healing-events?projectId={pid}").json() == []
  assert admin.post("/api/healing-events/missing/dismiss").status_code == 404
  assert admin.get(f"/api/projects/{pid}/dashboard").json()["healedLocatorCount"] == 2


def test_visual_diffs(admin: httpx.Client, project: dict, db: Db, admin_user: dict) -> None:
  pid = project["id"]
  tc_id = seed_test_case(db, pid, admin_user["id"])
  step_id, _, result_id = _seed_run_result(db, tc_id)

  vr_dir = visual_regression_dir()
  if vr_dir is None:
    pytest.skip("CONTRACT_VISUAL_REGRESSION_DIR not set")
  from PIL import Image

  (vr_dir / "diffs").mkdir(parents=True, exist_ok=True)
  actual_name = f"diffs/{result_id}-actual.png"
  Image.new("RGBA", (8, 6), (255, 0, 0, 255)).save(vr_dir / actual_name)

  ids = []
  for _ in range(2):
    diff_id = "vd" + uuid.uuid4().hex[:20]
    db.execute(
      'INSERT INTO "VisualDiff" (id, "testStepId", "testRunResultId", "baselinePath", "actualPath", "diffPath", "diffPercent", status, "createdAt") '
      "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
      (diff_id, step_id, result_id, "baselines/old.png", actual_name, actual_name, 12.5, "pending", _now()),
    )
    ids.append(diff_id)

  pending = admin.get(f"/api/visual-diffs?projectId={pid}").json()
  assert {d["id"] for d in pending} == set(ids)
  assert set(pending[0]) == {
    "id", "testStepId", "testRunResultId", "baselinePath", "actualPath", "diffPath", "diffPercent", "status", "createdAt", "resolvedAt",
  }
  assert admin.post(f"/api/visual-diffs/{ids[1]}/reject").json()["status"] == "rejected"
  approved = admin.post(f"/api/visual-diffs/{ids[0]}/approve")
  assert approved.status_code == 200 and approved.json()["status"] == "approved"
  assert (vr_dir / "baselines" / f"{step_id}-baseline.png").exists()
  row = db.execute('SELECT "filePath", width, height FROM "VisualBaseline" WHERE "testStepId" = %s', (step_id,))
  assert row == [(f"baselines/{step_id}-baseline.png", 8, 6)]
  assert admin.get(f"/api/visual-diffs?projectId={pid}").json() == []
  assert admin.post("/api/visual-diffs/missing/reject").status_code == 404
