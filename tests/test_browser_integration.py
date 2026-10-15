"""Drives a real browser through Playwright MCP (no LLM involved).

Needs `npm install` in this repo (for `@playwright/mcp`) and
`npx playwright install chromium`. Skip with `-m "not browser"`.
"""

from __future__ import annotations

import functools
import http.server
import threading
from dataclasses import replace
from pathlib import Path
from typing import Iterator

import pytest

from aegisqa.agents.browser import BrowserSessionManager, mcp_result_for_model
from aegisqa.config import settings

pytestmark = pytest.mark.browser

LOGIN_PAGE = """<!doctype html><html><head><title>Login</title></head><body>
<form><label for=e>Email</label><input id=e type=email>
<button type=button onclick="localStorage.setItem('seen','1')">Sign in</button></form>
</body></html>"""


@pytest.fixture
def site(tmp_path: Path) -> Iterator[str]:
  (tmp_path / "login.html").write_text(LOGIN_PAGE)
  handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
  server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  yield f"http://127.0.0.1:{server.server_address[1]}"
  server.shutdown()


async def test_real_browser_session_navigate_snapshot_and_state(site: str, tmp_path: Path) -> None:
  browsers = BrowserSessionManager(replace(settings, artifacts_dir=tmp_path / "artifacts", playwright_mcp_url=""))
  key = "chat:project:user"
  try:
    toolset = await browsers.get(key)
    names = {tool.name for tool in await toolset.get_tools()}
    assert {"browser_navigate", "browser_snapshot", "browser_click", "browser_storage_state"} <= names

    navigated = mcp_result_for_model(await toolset.call_tool("browser_navigate", {"url": f"{site}/login.html"}))
    assert navigated["isError"] is False

    snapshot = await browsers.snapshot(key)
    assert 'button "Sign in"' in snapshot
    assert "Email" in snapshot

    state, url = await browsers.capture_state(key)
    assert url == f"{site}/login.html"
    assert state is not None and "cookies" in state
  finally:
    await browsers.close_all()
  assert not browsers.has_session(key)
