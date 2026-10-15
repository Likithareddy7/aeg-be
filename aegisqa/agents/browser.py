"""Browser sessions for the agents, via Playwright MCP and ADK's McpToolset.

One `BrowserToolset` per session key (e.g. `chat:<projectId>:<userId>`), so
every user's chat exploration gets its own real, isolated browser that
survives across chat turns, and is closed after a period of inactivity.

Local dev: each session spawns `npx @playwright/mcp` over stdio.
GCP: set PLAYWRIGHT_MCP_URL to a Playwright MCP server reachable over
Streamable HTTP (e.g. a sidecar container) and no subprocess is spawned.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams, StreamableHTTPConnectionParams
from mcp import StdioServerParameters

from ..config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BrowserConfig:
  browser: Optional[str] = None
  headless: Optional[bool] = None
  viewport_width: Optional[int] = None
  viewport_height: Optional[int] = None


def sanitize_key_for_path(key: str) -> str:
  return re.sub(r"[^a-zA-Z0-9_-]", "_", key)


def mcp_result_to_text(result: dict[str, Any]) -> str:
  """Plain-text rendering of a dumped MCP CallToolResult (text blocks only)."""
  blocks = result.get("content") or []
  return "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text" and b.get("text"))


def mcp_result_is_error(result: dict[str, Any]) -> bool:
  return bool(result.get("isError") or result.get("is_error") or "error" in result and "content" not in result)


def mcp_result_for_model(result: dict[str, Any]) -> dict[str, Any]:
  """What the model sees for a browser tool call: text only.

  Raw MCP results carry base64 screenshots; forwarding those as JSON text
  would cost thousands of tokens for nothing the model can use, so they are
  replaced with a short marker (same behaviour as the old Node agent loop).
  """
  if "content" not in result and "error" in result:
    # ADK's McpTool turns transport/tool exceptions into {"error": "..."}.
    return {"content": f"Tool execution failed: {result['error']}", "isError": True}
  text = mcp_result_to_text(result).strip()
  if not text:
    has_image = any(isinstance(b, dict) and b.get("type") == "image" for b in result.get("content") or [])
    text = "(tool returned image content only)" if has_image else "(tool returned no content)"
  return {"content": text, "isError": mcp_result_is_error(result)}


def extract_evaluate_result_value(text: str) -> str:
  """`browser_evaluate` wraps the JSON-encoded return value between a
  "### Result" heading and a "### Ran Playwright code" heading that echoes
  the function source; pull out just the value."""
  after = re.split(r"###\s*Result", text, maxsplit=1, flags=re.IGNORECASE)
  section = after[1] if len(after) > 1 else text
  section = re.split(r"###\s*Ran Playwright code", section, maxsplit=1, flags=re.IGNORECASE)[0].strip()
  try:
    parsed = json.loads(section)
  except ValueError:
    return section
  return parsed if isinstance(parsed, str) else json.dumps(parsed)


class BrowserToolset(McpToolset):
  """McpToolset that can also be called directly (outside an agent turn),
  for snapshots, storage-state capture and page-URL reads."""

  def __init__(self, *, key: str, output_dir: Path, **kwargs: Any):
    super().__init__(**kwargs)
    self.key = key
    self.output_dir = output_dir
    self.last_used_at = time.monotonic()

  async def call_tool(self, name: str, args: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    self.last_used_at = time.monotonic()
    result = await self._execute_with_session(
      lambda session: session.call_tool(name, arguments=args or {}),
      f"Failed to call {name}",
    )
    return result.model_dump(mode="json", by_alias=True, exclude_none=True)


class BrowserSessionManager:
  def __init__(self, settings: Settings):
    self._settings = settings
    self._sessions: dict[str, BrowserToolset] = {}
    self._lock = asyncio.Lock()
    self._reaper: Optional[asyncio.Task[None]] = None

  def has_session(self, key: str) -> bool:
    return key in self._sessions

  def output_dir(self, key: str) -> Path:
    return self._settings.artifacts_dir / sanitize_key_for_path(key)

  async def get(self, key: str, config: Optional[BrowserConfig] = None) -> BrowserToolset:
    async with self._lock:
      toolset = self._sessions.get(key)
      if toolset is None:
        toolset = self._create(key, config or BrowserConfig())
        self._sessions[key] = toolset
        self._ensure_reaper()
      toolset.last_used_at = time.monotonic()
      return toolset

  def _create(self, key: str, config: BrowserConfig) -> BrowserToolset:
    output_dir = self.output_dir(key)
    output_dir.mkdir(parents=True, exist_ok=True)
    timeout = self._settings.browser_tool_timeout_seconds

    if self._settings.playwright_mcp_url:
      logger.info('browser: connecting session "%s" to %s', key, self._settings.playwright_mcp_url)
      connection: Any = StreamableHTTPConnectionParams(
        url=self._settings.playwright_mcp_url,
        timeout=timeout,
        sse_read_timeout=timeout,
      )
    else:
      logger.info('browser: starting Playwright MCP session "%s"', key)
      args = [
        "-y",
        "@playwright/mcp",
        "--browser",
        config.browser or self._settings.playwright_browser,
        "--output-dir",
        str(output_dir),
        # "devtools" enables trace/video tools; "storage" enables
        # browser_storage_state (used by "continue from chat session").
        "--caps",
        "devtools,storage",
        "--isolated",
      ]
      headless = self._settings.playwright_headless if config.headless is None else config.headless
      if headless:
        args.append("--headless")
      if config.viewport_width and config.viewport_height:
        args += ["--viewport-size", f"{config.viewport_width}x{config.viewport_height}"]
      npx = shutil.which("npx.cmd" if sys.platform == "win32" else "npx") or "npx"
      connection = StdioConnectionParams(
        server_params=StdioServerParameters(command=npx, args=args),
        timeout=timeout,
      )

    return BrowserToolset(key=key, output_dir=output_dir, connection_params=connection)

  async def close(self, key: str) -> None:
    async with self._lock:
      toolset = self._sessions.pop(key, None)
    if toolset is not None:
      logger.info('browser: closing session "%s"', key)
      await toolset.close()

  async def close_all(self) -> None:
    if self._reaper is not None:
      self._reaper.cancel()
      self._reaper = None
    for key in list(self._sessions):
      await self.close(key)

  def _ensure_reaper(self) -> None:
    if self._reaper is None or self._reaper.done():
      self._reaper = asyncio.create_task(self._reap_idle())

  async def _reap_idle(self) -> None:
    idle = self._settings.browser_idle_timeout_seconds
    while self._sessions:
      await asyncio.sleep(min(60.0, idle))
      now = time.monotonic()
      for key, toolset in list(self._sessions.items()):
        if now - toolset.last_used_at >= idle:
          await self.close(key)

  # -- helpers used by the HTTP API -----------------------------------------

  async def snapshot(self, key: str) -> str:
    toolset = await self.get(key)
    return mcp_result_to_text(await toolset.call_tool("browser_snapshot"))

  async def capture_state(self, key: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Best-effort storage state (cookies + localStorage) and current URL of
    a live session. (None, None) on any failure -- callers treat that as
    "nothing to inherit"."""
    toolset = self._sessions.get(key)
    if toolset is None:
      return None, None
    state: Optional[dict[str, Any]] = None
    url: Optional[str] = None
    since = time.time() - 60
    try:
      result = await toolset.call_tool("browser_storage_state")
      if not mcp_result_is_error(result):
        candidates = [
          p
          for p in toolset.output_dir.glob("storage-state*.json")
          if p.stat().st_mtime >= since
        ]
        if candidates:
          newest = max(candidates, key=lambda p: p.stat().st_mtime)
          state = json.loads(newest.read_text("utf-8"))
    except Exception as err:  # noqa: BLE001 -- best-effort by design
      logger.warning('browser: storage state capture failed for "%s": %s', key, err)
    try:
      result = await toolset.call_tool("browser_evaluate", {"function": "() => location.href"})
      if not mcp_result_is_error(result):
        url = extract_evaluate_result_value(mcp_result_to_text(result)) or None
    except Exception as err:  # noqa: BLE001
      logger.warning('browser: page URL capture failed for "%s": %s', key, err)
    return state, url
