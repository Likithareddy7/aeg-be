from __future__ import annotations

import io
import zipfile

import httpx

from .conftest import Db, Users, add_member, assert_keys, seed_test_case, unique

PROJECT_KEYS = {"id", "name", "createdAt", "urls", "myRole"}


def test_project_crud_and_listing(admin: httpx.Client, users: Users) -> None:
  created = admin.post("/api/projects", json={"name": unique("P")})
  assert created.status_code == 201
  project = created.json()
  assert set(project) == PROJECT_KEYS and project["urls"] == [] and project["myRole"] == "owner"
  assert admin.post("/api/projects", json={"name": ""}).status_code == 400

  assert any(p["id"] == project["id"] and p["myRole"] == "owner" for p in admin.get("/api/projects").json())
  assert admin.get(f"/api/projects/{project['id']}").json()["myRole"] == "owner"

  renamed = admin.put(f"/api/projects/{project['id']}", json={"name": "Renamed"})
  assert renamed.status_code == 200 and renamed.json()["name"] == "Renamed"

  member, member_client = users.create()
  # Not a member: invisible in the list, 403 on direct access, 404 if missing.
  assert all(p["id"] != project["id"] for p in member_client.get("/api/projects").json())
  assert member_client.get(f"/api/projects/{project['id']}").status_code == 403
  assert member_client.get("/api/projects/does-not-exist").status_code == 404

  add_member(admin, project["id"], member["username"], "editor")
  listed = [p for p in member_client.get("/api/projects").json() if p["id"] == project["id"]]
  assert listed and listed[0]["myRole"] == "editor"
  assert member_client.put(f"/api/projects/{project['id']}", json={"name": "x"}).status_code == 403
  assert member_client.delete(f"/api/projects/{project['id']}").status_code == 403

  assert admin.delete(f"/api/projects/{project['id']}").status_code == 204
  assert admin.get(f"/api/projects/{project['id']}").status_code == 404


def test_membership_management_and_audit_log(admin: httpx.Client, users: Users, project: dict) -> None:
  pid = project["id"]
  member, member_client = users.create()

  available = admin.get(f"/api/projects/{pid}/available-members").json()
  assert any(u["id"] == member["id"] for u in available)

  missing = admin.post(f"/api/projects/{pid}/members", json={"username": "no-such-user", "role": "viewer"})
  assert missing.status_code == 404
  bad_role = admin.post(f"/api/projects/{pid}/members", json={"username": member["username"], "role": "god"})
  assert bad_role.status_code == 400

  added = admin.post(f"/api/projects/{pid}/members", json={"username": member["username"], "role": "viewer"})
  assert added.status_code == 201
  record = added.json()
  assert set(record) == {"id", "projectId", "user", "role", "createdAt"} and record["role"] == "viewer"
  assert all(u["id"] != member["id"] for u in admin.get(f"/api/projects/{pid}/available-members").json())

  members = admin.get(f"/api/projects/{pid}/members").json()
  assert [m["role"] for m in members if m["user"]["id"] == member["id"]] == ["viewer"]

  # Viewers can read but not manage.
  assert member_client.get(f"/api/projects/{pid}/members").status_code == 200
  assert member_client.get(f"/api/projects/{pid}/available-members").status_code == 403

  updated = admin.put(f"/api/projects/{pid}/members/{member['id']}", json={"role": "reviewer"})
  assert updated.status_code == 200 and updated.json()["role"] == "reviewer"
  assert admin.put(f"/api/projects/{pid}/members/not-a-member", json={"role": "viewer"}).status_code == 404

  assert admin.delete(f"/api/projects/{pid}/members/{member['id']}").status_code == 204
  assert member_client.get(f"/api/projects/{pid}").status_code == 403

  actions = [e["action"] for e in admin.get(f"/api/projects/{pid}/audit-log").json()]
  for expected in ["project.created", "member.added", "member.role_changed", "member.removed"]:
    assert expected in actions
  entry = admin.get(f"/api/projects/{pid}/audit-log").json()[0]
  assert set(entry) == {"id", "userId", "username", "projectId", "action", "targetType", "targetId", "detail", "createdAt"}
  assert entry["action"] == "member.removed"  # newest first


def test_add_url(admin: httpx.Client, users: Users, project: dict, site: str) -> None:
  pid = project["id"]
  ok = admin.post(f"/api/projects/{pid}/urls", json={"url": f"{site}/login.html"})
  assert ok.status_code == 201
  body = ok.json()
  assert body["reachable"] is True and body["reachabilityNote"] is None
  assert set(body["url"]) == {"id", "projectId", "url", "title", "lastInspectedAt"}

  unreachable = admin.post(f"/api/projects/{pid}/urls", json={"url": "http://127.0.0.1:9/nothing"}).json()
  assert unreachable["reachable"] is False and unreachable["reachabilityNote"]

  assert admin.post(f"/api/projects/{pid}/urls", json={"url": "not a url"}).status_code == 400
  assert len(admin.get(f"/api/projects/{pid}").json()["urls"]) == 2

  viewer, viewer_client = users.create()
  add_member(admin, pid, viewer["username"], "viewer")
  assert viewer_client.post(f"/api/projects/{pid}/urls", json={"url": site}).status_code == 403


def test_per_user_read_endpoints_start_empty(admin: httpx.Client, project: dict) -> None:
  pid = project["id"]
  assert admin.get(f"/api/projects/{pid}/chat").json() == []
  assert admin.get(f"/api/projects/{pid}/agent-activity").json() == []
  assert admin.get(f"/api/projects/{pid}/testcases").json() == []
  assert admin.get(f"/api/projects/{pid}/usage").json() == {
    "inputTokens": 0,
    "outputTokens": 0,
    "cacheReadTokens": 0,
    "cacheWriteTokens": 0,
    "costUsd": 0,
  }
  assert admin.get(f"/api/projects/{pid}/failing-tests").json() == []


def test_dashboard_and_reports_shapes(admin: httpx.Client, project: dict, db: Db, admin_user: dict) -> None:
  pid = project["id"]
  seed_test_case(db, pid, admin_user["id"], status="approved", module="Checkout")
  seed_test_case(db, pid, admin_user["id"], module=None)
  dash = admin.get(f"/api/projects/{pid}/dashboard").json()
  assert_keys(
    dash,
    {
      "totalTestCases", "testCasesByStatus", "testCasesByType", "recentRuns", "passCount", "failCount",
      "skipCount", "flakyTestCaseIds", "averageDurationMs", "averageDurationSampleSize", "releaseReadinessScore",
      "openChangeRequestCount", "openChangeRequests", "testHealthScore", "automationCoveragePercent",
      "timeSavedHours", "timeSavedUsd", "healedLocatorCount", "maxParallelRuns", "trend",
      "failingTestsByPriority", "failingTests", "coverageByModule", "recentActivity",
    },
  )
  assert dash["totalTestCases"] == 2
  assert dash["testCasesByStatus"] == {"approved": 1, "draft": 1}
  assert dash["automationCoveragePercent"] == 50
  assert dash["failingTestsByPriority"] == {"critical": 0, "high": 0, "medium": 0, "low": 0}
  assert dash["trend"] == {"testHealthDelta": None, "automationCoverageDelta": None, "releaseReadinessDelta": None}
  assert sorted(m["module"] for m in dash["coverageByModule"]) == ["Checkout", "Unassigned"]
  assert all(m["passRate"] is None for m in dash["coverageByModule"])
  assert [a["kind"] for a in dash["recentActivity"]] == ["generated", "generated"]
  assert dash["recentActivity"][0]["href"].startswith(f"/project/{pid}/tests/")

  reports = admin.get(f"/api/projects/{pid}/reports").json()
  assert set(reports) == {"timeSeriesDays"} and len(reports["timeSeriesDays"]) == 30
  assert set(reports["timeSeriesDays"][-1]) == {"date", "passed", "failed", "skipped", "avgDurationMs"}


def test_environments(admin: httpx.Client, users: Users, project: dict) -> None:
  pid = project["id"]
  payload = {
    "name": "staging",
    "baseUrl": "https://staging.example.test",
    "browser": "chromium",
    "headless": True,
    "viewportWidth": 1280,
    "viewportHeight": 720,
    "isDefault": True,
  }
  assert admin.post(f"/api/projects/{pid}/environments", json={**payload, "browser": "lynx"}).status_code == 400
  created = admin.post(f"/api/projects/{pid}/environments", json=payload)
  assert created.status_code == 201
  env = created.json()
  assert set(env) == {*payload, "id", "projectId", "hasCredentials", "createdAt", "updatedAt"}
  assert env["hasCredentials"] is False

  updated = admin.put(f"/api/environments/{env['id']}", json={"viewportWidth": 1920})
  assert updated.status_code == 200 and updated.json()["viewportWidth"] == 1920 and updated.json()["name"] == "staging"

  creds = admin.put(f"/api/environments/{env['id']}/credentials", json={"values": {"PASSWORD": "s3cret"}})
  assert creds.status_code == 200 and creds.json()["hasCredentials"] is True
  assert "s3cret" not in creds.text
  assert admin.put(f"/api/environments/{env['id']}/credentials", json={"values": "x"}).status_code == 400
  assert [e["id"] for e in admin.get(f"/api/projects/{pid}/environments").json()] == [env["id"]]

  editor, editor_client = users.create()
  add_member(admin, pid, editor["username"], "editor")
  assert editor_client.put(f"/api/environments/{env['id']}", json={"name": "qa"}).status_code == 200
  assert editor_client.put(f"/api/environments/{env['id']}/credentials", json={"values": {}}).status_code == 403
  assert editor_client.delete(f"/api/environments/{env['id']}").status_code == 403

  assert admin.put("/api/environments/missing", json={"name": "x"}).status_code == 404
  assert admin.delete(f"/api/environments/{env['id']}").status_code == 204
  assert admin.get(f"/api/projects/{pid}/environments").json() == []
  actions = [e["action"] for e in admin.get(f"/api/projects/{pid}/audit-log").json()]
  assert "environment.credentials_updated" in actions and "environment.deleted" in actions


def test_azure_devops_is_the_only_integration(admin: httpx.Client, users: Users, project: dict) -> None:
  pid = project["id"]
  assert admin.get(f"/api/projects/{pid}/integrations").json() == {"azureDevOps": None}

  first = {"baseUrl": "https://dev.azure.com/acme", "email": "", "projectKey": "QA"}
  assert admin.put(f"/api/projects/{pid}/integrations/azureDevOps", json=first).status_code == 400  # token required first time
  saved = admin.put(f"/api/projects/{pid}/integrations/azureDevOps", json={**first, "apiToken": "pat-123"})
  assert saved.status_code == 200
  summary = saved.json()
  assert set(summary) == {"id", "projectId", "type", "baseUrl", "email", "projectKey", "hasApiToken", "createdAt", "updatedAt"}
  assert summary["type"] == "azureDevOps" and summary["hasApiToken"] is True and "pat-123" not in saved.text

  # Token optional on later updates.
  again = admin.put(f"/api/projects/{pid}/integrations/azureDevOps", json={**first, "projectKey": "QA2"})
  assert again.status_code == 200 and again.json()["projectKey"] == "QA2"
  assert admin.get(f"/api/projects/{pid}/integrations").json()["azureDevOps"]["projectKey"] == "QA2"

  for removed in ["jira", "githubIssues"]:
    assert admin.put(f"/api/projects/{pid}/integrations/{removed}", json={**first, "apiToken": "x"}).status_code == 400

  editor, editor_client = users.create()
  add_member(admin, pid, editor["username"], "editor")
  assert editor_client.put(f"/api/projects/{pid}/integrations/azureDevOps", json=first).status_code == 403


def test_export_ci_package(admin: httpx.Client, project: dict, db: Db, admin_user: dict) -> None:
  pid = project["id"]
  seed_test_case(db, pid, admin_user["id"], status="approved", title="Approved login check")
  seed_test_case(db, pid, admin_user["id"], status="draft", title="Draft only")
  response = admin.get(f"/api/projects/{pid}/export-ci")
  assert response.status_code == 200
  assert response.headers["content-type"].startswith("application/zip")
  assert 'filename="aegisqa-ci-export.zip"' in response.headers["content-disposition"]
  names = sorted(zipfile.ZipFile(io.BytesIO(response.content)).namelist())
  assert names == ["README.md", "package.json", "playwright.config.ts", "tests/approved-login-check.spec.ts"]
