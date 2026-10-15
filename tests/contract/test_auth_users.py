from __future__ import annotations

import re

import httpx

from .conftest import ISO_RE, Users, login, new_client, unique


def test_login_sets_session_cookie_and_me_returns_user(admin_user: dict) -> None:
  assert set(admin_user) == {"id", "username", "role"}
  assert admin_user["role"] == "admin"


def test_login_cookie_attributes() -> None:
  from .conftest import ADMIN_PASSWORD, ADMIN_USERNAME

  with new_client() as client:
    response = client.post("/api/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
  cookie = response.headers["set-cookie"].lower()
  assert cookie.startswith("sid=")
  assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie and "expires=" in cookie


def test_login_rejects_bad_credentials_and_missing_fields() -> None:
  with new_client() as client:
    assert client.post("/api/auth/login", json={"username": "nobody", "password": "x"}).status_code == 401
    bad = client.post("/api/auth/login", json={"username": ""})
    assert bad.status_code == 400
    assert "error" in bad.json()


def test_unauthenticated_requests_get_401() -> None:
  with new_client() as client:
    for path in ["/api/auth/me", "/api/projects", "/api/users", "/api/admin/config"]:
      response = client.get(path)
      assert response.status_code == 401, path
      assert response.json() == {"error": "Not authenticated"}


def test_logout_invalidates_session(users: Users) -> None:
  _, client = users.create()
  assert client.get("/api/auth/me").status_code == 200
  response = client.post("/api/auth/logout")
  assert response.status_code == 204
  # The server-side session is gone even if the cookie is replayed.
  assert client.get("/api/auth/me").status_code == 401


def test_change_own_password_keeps_current_session_and_ends_others(users: Users) -> None:
  user, client = users.create()
  other = login(user["username"], user["password"])
  wrong = client.put("/api/auth/password", json={"currentPassword": "nope", "newPassword": "newpass123"})
  assert wrong.status_code == 401
  short = client.put("/api/auth/password", json={"currentPassword": user["password"], "newPassword": "123"})
  assert short.status_code == 400
  ok = client.put("/api/auth/password", json={"currentPassword": user["password"], "newPassword": "newpass123"})
  assert ok.status_code == 204
  assert client.get("/api/auth/me").status_code == 200
  assert other.get("/api/auth/me").status_code == 401
  login(user["username"], "newpass123").close()
  other.close()


def test_admin_user_management(admin: httpx.Client, admin_user: dict, users: Users) -> None:
  member, member_client = users.create()
  listed = admin.get("/api/users").json()
  assert any(u["id"] == member["id"] and u["role"] == "member" for u in listed)
  assert all(set(u) == {"id", "username", "role"} for u in listed)

  dup = admin.post("/api/users", json={"username": member["username"], "password": "abcdef"})
  assert dup.status_code == 409
  assert admin.post("/api/users", json={"username": unique("u"), "password": "123"}).status_code == 400

  promoted = admin.put(f"/api/users/{member['id']}", json={"role": "admin"})
  assert promoted.status_code == 200 and promoted.json()["role"] == "admin"
  # A role-only change keeps sessions; a password reset ends them.
  assert member_client.get("/api/auth/me").status_code == 200
  reset = admin.put(f"/api/users/{member['id']}", json={"password": "resetpass1"})
  assert reset.status_code == 200
  assert member_client.get("/api/auth/me").status_code == 401

  assert admin.put(f"/api/users/{member['id']}", json={}).status_code == 400
  assert admin.put("/api/users/does-not-exist", json={"role": "member"}).status_code == 404
  assert admin.put(f"/api/users/{admin_user['id']}", json={"role": "member"}).status_code == 400


def test_member_cannot_manage_users_or_read_admin_config(users: Users) -> None:
  _, client = users.create()
  assert client.get("/api/users").status_code == 403
  assert client.post("/api/users", json={"username": unique("x"), "password": "abcdef"}).status_code == 403
  assert client.put("/api/users/whatever", json={"role": "admin"}).status_code == 403
  assert client.get("/api/admin/config").status_code == 403


def test_admin_config_shape(admin: httpx.Client) -> None:
  config = admin.get("/api/admin/config").json()
  assert set(config) == {
    "model",
    "credentialEncryptionConfigured",
    "defaultBrowser",
    "defaultHeadless",
    "maxParallelRuns",
    "webhookConfigured",
  }
  assert isinstance(config["model"], str) and isinstance(config["maxParallelRuns"], int)


def test_health_endpoint() -> None:
  with new_client() as client:
    body = client.get("/api/health").json()
  assert body["ok"] is True


def test_timestamps_are_js_iso_format(admin: httpx.Client) -> None:
  project = admin.post("/api/projects", json={"name": unique("ts")}).json()
  assert re.match(ISO_RE, project["createdAt"]), project["createdAt"]
