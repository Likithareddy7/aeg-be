"""Runs the real app in-process (uvicorn in a thread) against TEST_DATABASE_URL,
with an injected model and browser manager."""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from .conftest import TEST_DATABASE_URL

ROOT = Path(__file__).resolve().parents[1]
ADMIN_PASSWORD = "admin-pass-123"

requires_test_database = pytest.mark.skipif(
  not TEST_DATABASE_URL or "test" not in TEST_DATABASE_URL.rsplit("/", 1)[-1],
  reason="set TEST_DATABASE_URL to a database whose name contains 'test'",
)


def reset_database() -> None:
  import psycopg

  with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")
  subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, check=True, capture_output=True)


def sql(statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
  import psycopg

  with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
    cur = conn.execute(statement, params)
    return cur.fetchall() if cur.description else []


def _free_port() -> int:
  with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    return sock.getsockname()[1]


class RunningApp:
  def __init__(
    self, model: Any, browsers: Any, embedder: Any = None, artifacts: Any = None, publisher: Any = None, verifier: Any = None
  ):
    import uvicorn

    from aegisqa.auth.passwords import hash_password
    from aegisqa.main import create_app

    reset_database()
    sql(
      'INSERT INTO "User" (id, username, "passwordHash", role) VALUES (%s, %s, %s, %s)',
      ("admin-id", "admin", hash_password(ADMIN_PASSWORD), "admin"),
    )
    self.model, self.browsers = model, browsers
    from .fakes import FakeEmbedder

    app = create_app(
      model=model,
      browsers=browsers,
      embedder=embedder or FakeEmbedder(),
      artifacts=artifacts,
      publisher=publisher,
      internal_verifier=verifier,
    )
    self.port = _free_port()
    self.base = f"http://127.0.0.1:{self.port}"
    self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
    self._thread = threading.Thread(target=self._server.run, daemon=True)
    self._thread.start()
    for _ in range(200):
      if self._server.started:
        break
      time.sleep(0.05)

  def client(self, username: str = "admin", password: str = ADMIN_PASSWORD) -> httpx.Client:
    client = httpx.Client(base_url=self.base, timeout=120)
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return client

  def stop(self) -> None:
    self._server.should_exit = True
    self._thread.join(timeout=10)
