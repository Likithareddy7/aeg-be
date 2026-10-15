"""Black-box API contract tests.

They talk to a *running* backend over HTTP/WebSocket and never import its
code, so the same suite verifies any implementation -- it was first run
against the original Node backend, then against this Python one, to prove
the rewrite kept the API the frontend depends on.

Required env vars:
  CONTRACT_BASE_URL        e.g. http://localhost:4100
  CONTRACT_DATABASE_URL    the backend's Postgres (used only to seed rows the
                           API has no endpoint to create, e.g. test cases --
                           in the app those come from the agent)
  CONTRACT_ADMIN_USERNAME / CONTRACT_ADMIN_PASSWORD
Optional:
  CONTRACT_VISUAL_REGRESSION_DIR   the backend's VISUAL_REGRESSION_DIR (enables
                                   the visual-diff approve test)

Run: pytest -m contract tests/contract
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import secrets
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import httpx
import pytest

BASE_URL = os.environ.get("CONTRACT_BASE_URL", "")
DATABASE_URL = os.environ.get("CONTRACT_DATABASE_URL", "")
ADMIN_USERNAME = os.environ.get("CONTRACT_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("CONTRACT_ADMIN_PASSWORD", "")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
  for item in items:
    if "tests/contract" in str(item.fspath):
      item.add_marker(pytest.mark.contract)
      if not BASE_URL:
        item.add_marker(pytest.mark.skip(reason="CONTRACT_BASE_URL not set"))


ISO_RE = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"


def unique(prefix: str) -> str:
  return f"{prefix}-{secrets.token_hex(4)}"


def new_client() -> httpx.Client:
  return httpx.Client(base_url=BASE_URL, timeout=180)


def login(username: str, password: str) -> httpx.Client:
  client = new_client()
  response = client.post("/api/auth/login", json={"username": username, "password": password})
  assert response.status_code == 200, response.text
  return client


@pytest.fixture(scope="session")
def admin() -> Iterator[httpx.Client]:
  client = login(ADMIN_USERNAME, ADMIN_PASSWORD)
  yield client
  client.close()


@pytest.fixture(scope="session")
def admin_user(admin: httpx.Client) -> dict[str, Any]:
  return admin.get("/api/auth/me").json()


class Users:
  """Creates throwaway users through the admin API and logs them in."""

  def __init__(self, admin: httpx.Client):
    self._admin = admin
    self._clients: list[httpx.Client] = []

  def create(self, role: str = "member") -> tuple[dict[str, Any], httpx.Client]:
    username = unique("user")
    password = "pw-" + secrets.token_hex(6)
    response = self._admin.post("/api/users", json={"username": username, "password": password, "role": role})
    assert response.status_code == 201, response.text
    client = login(username, password)
    self._clients.append(client)
    user = response.json()
    user["password"] = password
    return user, client

  def close(self) -> None:
    for client in self._clients:
      client.close()


@pytest.fixture
def users(admin: httpx.Client) -> Iterator[Users]:
  registry = Users(admin)
  yield registry
  registry.close()


@pytest.fixture
def project(admin: httpx.Client) -> dict[str, Any]:
  response = admin.post("/api/projects", json={"name": unique("Contract project")})
  assert response.status_code == 201, response.text
  return response.json()


def add_member(admin: httpx.Client, project_id: str, username: str, role: str) -> None:
  response = admin.post(f"/api/projects/{project_id}/members", json={"username": username, "role": role})
  assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# Direct DB seeding for rows the API can't create.
# ---------------------------------------------------------------------------


def _now() -> datetime:
  return datetime.now(timezone.utc).replace(tzinfo=None)


class Db:
  def __init__(self, url: str):
    import psycopg

    self._conn = psycopg.connect(url, autocommit=True)

  def execute(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with self._conn.cursor() as cur:
      cur.execute(sql, params)
      return cur.fetchall() if cur.description else []

  def close(self) -> None:
    self._conn.close()


@pytest.fixture(scope="session")
def db() -> Iterator[Db]:
  if not DATABASE_URL:
    pytest.skip("CONTRACT_DATABASE_URL not set")
  conn = Db(DATABASE_URL)
  yield conn
  conn.close()


def selector(role: str, name: str, description: Optional[str] = None) -> dict[str, Any]:
  return {"strategy": "role", "role": role, "name": name, "description": description or f"{name} {role}"}


def default_steps(site: str) -> list[dict[str, Any]]:
  return [
    {"order": 1, "action": "navigate", "value": f"{site}/login.html", "description": "Open login", "selector": None},
    {"order": 2, "action": "fill", "value": "{{data.email}}", "description": "Type email", "selector": selector("textbox", "Email")},
    {"order": 3, "action": "click", "value": None, "description": "Submit", "selector": selector("button", "Sign in")},
    {"order": 4, "action": "assertText", "value": "Welcome back", "description": "Welcome shown", "selector": None},
  ]


def seed_test_case(
  db: Db,
  project_id: str,
  created_by: Optional[str],
  *,
  steps: Optional[list[dict[str, Any]]] = None,
  status: str = "draft",
  title: Optional[str] = None,
  url_id: Optional[str] = None,
  module: Optional[str] = "Authentication",
) -> str:
  tc_id = "tc" + uuid.uuid4().hex[:20]
  now = _now()
  db.execute(
    'INSERT INTO "TestCase" (id, "projectId", "urlId", title, objective, preconditions, "testData", "expectedResult", '
    'priority, type, status, module, "sourcePrompt", tags, version, "createdById", "lastModifiedById", "createdAt", "updatedAt") '
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
    (
      tc_id, project_id, url_id, title or unique("Seeded test"), "Verify login", "Logged out",
      json.dumps({"email": "a@b.test"}), "Welcome shown", "high", "smoke", status, module,
      "seeded by contract tests", "[]", 1, created_by, created_by, now, now,
    ),
  )
  for step in steps if steps is not None else default_steps("http://127.0.0.1:1"):
    db.execute(
      'INSERT INTO "TestStep" (id, "testCaseId", "order", action, selector, "locatorCandidates", value, description, enabled, "sourceFlowVersionId") '
      "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
      (
        "ts" + uuid.uuid4().hex[:20], tc_id, step["order"], step["action"],
        json.dumps(step["selector"]) if step.get("selector") else None,
        json.dumps(step["locatorCandidates"]) if step.get("locatorCandidates") else None,
        step.get("value"), step["description"], step.get("enabled", True), step.get("sourceFlowVersionId"),
      ),
    )
  return tc_id


# ---------------------------------------------------------------------------
# A tiny local site for real browser runs, and a fake Azure DevOps API.
# ---------------------------------------------------------------------------

LOGIN_PAGE = """<!doctype html><html><head><title>Acme Login</title></head><body>
<h1>Sign in to Acme</h1>
<form onsubmit="event.preventDefault()"><label for=e>Email</label><input id=e type=email>
<button type=button onclick="document.getElementById('msg').textContent='Welcome back'">Sign in</button></form>
<p id=msg></p></body></html>"""


@pytest.fixture(scope="session")
def site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
  root = tmp_path_factory.mktemp("site")
  (root / "login.html").write_text(LOGIN_PAGE)
  handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
  server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
  threading.Thread(target=server.serve_forever, daemon=True).start()
  yield f"http://127.0.0.1:{server.server_address[1]}"
  server.shutdown()


class FakeAzureDevOps:
  def __init__(self) -> None:
    self.requests: list[dict[str, Any]] = []
    outer = self

    class Handler(http.server.BaseHTTPRequestHandler):
      def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        outer.requests.append(
          {
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "contentType": self.headers.get("Content-Type"),
            "body": json.loads(self.rfile.read(length) or b"null"),
          }
        )
        payload = json.dumps({"id": 4242}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

      def log_message(self, *args: Any) -> None:
        pass

    self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=self.server.serve_forever, daemon=True).start()
    self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def fake_ado() -> Iterator[FakeAzureDevOps]:
  fake = FakeAzureDevOps()
  yield fake
  fake.server.shutdown()


VISUAL_REGRESSION_DIR = os.environ.get("CONTRACT_VISUAL_REGRESSION_DIR", "")


def visual_regression_dir() -> Optional[Path]:
  return Path(VISUAL_REGRESSION_DIR) if VISUAL_REGRESSION_DIR else None


def assert_keys(obj: dict[str, Any], keys: set[str]) -> None:
  missing = keys - set(obj)
  assert not missing, f"missing keys {missing} in {sorted(obj)}"


Factory = Callable[..., Any]
