"""Runs a saved test case's steps in a fresh, isolated browser session.

A run stops at the first failing step (later steps almost always depend on
it) but is always finalized: diagnostics captured, browser closed, status
recorded. Every step's progress is emitted live over the WebSocket.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from sqlalchemy import select

from .. import chat, services
from ..agents.browser import BrowserConfig, BrowserToolset, extract_evaluate_result_value, mcp_result_is_error, mcp_result_to_text
from ..config import settings
from ..crypto import decrypt_secret
from ..db import models as m
from ..db import runs as run_records
from ..db.base import new_id, session_scope, utcnow
from ..memory.learning import remember
from .selectors import Resolved, extract_url_from_snapshot, resolve_element
from .visual import check_visual_match

logger = logging.getLogger(__name__)

Emit = Callable[[dict[str, Any]], None]

# Assertions right after an action that navigates (e.g. a submit click) can
# race the navigation, so they poll for a bounded window instead of failing fast.
ASSERTION_POLL_TIMEOUT = 8.0
ASSERTION_POLL_INTERVAL = 0.5

_ENV = re.compile(r"\{\{\s*env\.([\w-]+)\s*\}\}")
_DATA = re.compile(r"\{\{\s*data\.([\w-]+)\s*\}\}")
_MASK = "••••••"

# Lightweight accessible-name heuristic for `assertAccessible` -- not a full audit.
ACCESSIBLE_NAME_CHECK_FN = (
  "(element) => { "
  "const tag = element.tagName.toLowerCase(); "
  "const ariaLabel = element.getAttribute('aria-label') || ''; "
  "const labelledBy = element.getAttribute('aria-labelledby'); "
  "let labelledText = ''; "
  "if (labelledBy) { labelledText = labelledBy.split(' ').map(function (id) { var n = document.getElementById(id); return n ? n.textContent : ''; }).join(' '); } "
  "let name = ''; "
  "if (tag === 'img') { name = element.getAttribute('alt') || ''; } "
  "else if (tag === 'input' || tag === 'select' || tag === 'textarea') { var lbl = (element.labels && element.labels.length > 0) ? element.labels[0].textContent : ''; name = ariaLabel || labelledText || lbl || ''; } "
  "else { name = ariaLabel || labelledText || element.textContent || ''; } "
  "return String(!!name.trim()); "
  "}"
)


@dataclass
class DataRow:
  index: int
  values: dict[str, str]
  secret_columns: list[str]


@dataclass
class ExecuteOptions:
  environment_id: Optional[str] = None
  batch_id: Optional[str] = None
  # Seed the run with the triggering user's own chat browser's cookies/URL.
  continue_from_chat_session: bool = False
  user_id: Optional[str] = None
  # "Rerun from step N": steps before N are inherited from that run, not re-run.
  resume_from: Optional[tuple[str, int]] = None
  data_row: Optional[DataRow] = None


@dataclass
class _ExecEnvironment:
  base_url: str
  config: BrowserConfig
  credentials: dict[str, str] = field(default_factory=dict)


@dataclass
class _StepOutcome:
  text: str
  recovered: bool = False
  resolved: Optional[Resolved] = None


class StepFailure(Exception):
  pass


# ---------------------------------------------------------------------------
# Placeholders
# ---------------------------------------------------------------------------


def substitute(value: Optional[str], credentials: dict[str, str], row: dict[str, str]) -> Optional[str]:
  """`{{env.KEY}}` -> decrypted credential, `{{data.COLUMN}}` -> this run's
  data row. Unknown placeholders are left as-is."""
  if not value:
    return value
  value = _ENV.sub(lambda mt: credentials.get(mt.group(1), mt.group(0)), value)
  return _DATA.sub(lambda mt: row.get(mt.group(1), mt.group(0)), value)


def masked_display_value(template: Optional[str], row: dict[str, str], secret_columns: set[str]) -> str:
  """What a step's value looks like in stored results: credentials always
  masked, data columns masked only when flagged secret."""
  if not template:
    return template or ""
  masked = _ENV.sub(_MASK, template)
  masked = _DATA.sub(lambda mt: _MASK if mt.group(1) in secret_columns else mt.group(0), masked)
  return substitute(masked, {}, row) or ""


def new_batch_id() -> str:
  suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
  return f"batch_{int(time.time() * 1000)}_{suffix}"


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------


async def _call_checked(browser: BrowserToolset, name: str, args: dict[str, Any]) -> dict[str, Any]:
  """MCP reports tool-level failures (bad arguments, element not actionable)
  as a result flag, not an exception."""
  result = await browser.call_tool(name, args)
  if mcp_result_is_error(result):
    raise StepFailure(mcp_result_to_text(result) or f'Tool "{name}" reported an error.')
  return result


async def _safe_snapshot(browser: BrowserToolset) -> str:
  try:
    return mcp_result_to_text(await browser.call_tool("browser_snapshot"))
  except Exception:  # noqa: BLE001
    return "(no snapshot available)"


async def _evaluate_on(browser: BrowserToolset, description: str, ref: str, fn: str) -> str:
  result = await browser.call_tool("browser_evaluate", {"element": description, "target": ref, "function": fn})
  if mcp_result_is_error(result):
    raise StepFailure(mcp_result_to_text(result) or "browser_evaluate reported an error.")
  return extract_evaluate_result_value(mcp_result_to_text(result))


def _image_bytes(result: dict[str, Any]) -> Optional[bytes]:
  for block in result.get("content") or []:
    if isinstance(block, dict) and block.get("type") == "image" and block.get("data"):
      return base64.b64decode(block["data"])
  return None


def _newest_file(root: Path, suffix_or_prefix: Callable[[str], bool], since: float) -> Optional[Path]:
  best: Optional[tuple[float, Path]] = None
  if not root.exists():
    return None
  for path in root.rglob("*"):
    if path.is_file() and suffix_or_prefix(path.name):
      mtime = path.stat().st_mtime
      if mtime >= since and (best is None or mtime > best[0]):
        best = (mtime, path)
  return best[1] if best else None


async def _capture_screenshot(browser: BrowserToolset, result_id: str, step_order: int) -> Optional[str]:
  """Saves a PNG for the step and records it. Returns the stored path."""
  try:
    data = _image_bytes(await browser.call_tool("browser_take_screenshot", {"type": "png"}))
    if data is None:
      logger.warning("browser_take_screenshot returned no image for step %s", step_order)
      return None
    file_name = f"{result_id}-step{step_order}.png"
    await services.get().artifacts.put("screenshots", file_name, data)  # type: ignore[union-attr]
    async with session_scope() as db:
      db.add(m.Screenshot(id=new_id(), test_run_result_id=result_id, step_order=step_order, file_path=file_name))
    return file_name
  except Exception as err:  # noqa: BLE001 -- never fails the step
    logger.warning("failed to capture screenshot for step %s: %s", step_order, err)
    return None


async def _start_tracing(browser: BrowserToolset) -> None:
  for tool in ("browser_start_tracing", "browser_start_video"):
    try:
      await browser.call_tool(tool)
    except Exception as err:  # noqa: BLE001 -- unsupported -> no artifact this time
      logger.warning("%s failed: %s", tool, err)


async def _stop_tracing(browser: BrowserToolset) -> tuple[Optional[str], Optional[str]]:
  since = time.time() - 5 * 60
  paths: list[Optional[str]] = []
  for tool, suffix in (("browser_stop_tracing", ".trace"), ("browser_stop_video", ".webm")):
    try:
      await browser.call_tool(tool)
      found = _newest_file(browser.output_dir, lambda name, sfx=suffix: name.endswith(sfx), since)
      relative = None
      if found:
        relative = found.relative_to(settings.artifacts_dir).as_posix()
        await services.get().artifacts.put_file("artifacts", relative, found)  # type: ignore[union-attr]
      paths.append(relative)
    except Exception as err:  # noqa: BLE001
      logger.warning("%s failed: %s", tool, err)
      paths.append(None)
  return paths[0], paths[1]


async def _capture_diagnostics(browser: BrowserToolset, run_id: str) -> None:
  """Browser console and network logs, stored as ExecutionLog rows."""
  for tool, category in (("browser_console_messages", "console"), ("browser_network_requests", "network")):
    try:
      text = mcp_result_to_text(await browser.call_tool(tool))
      if text.strip():
        async with session_scope() as db:
          db.add(m.ExecutionLog(id=new_id(), test_run_id=run_id, level="info", category=category, message=text[:20000]))
    except Exception as err:  # noqa: BLE001
      logger.warning("failed to capture %s logs: %s", category, err)


async def _restore_storage_state(browser: BrowserToolset, state: dict[str, Any]) -> None:
  try:
    browser.output_dir.mkdir(parents=True, exist_ok=True)
    path = browser.output_dir / "inherited-storage-state.json"
    path.write_text(json.dumps(state))
    await browser.call_tool("browser_set_storage_state", {"filename": str(path)})
  except Exception as err:  # noqa: BLE001 -- the run just starts logged out
    logger.warning("storage state restore failed: %s", err)


async def _log(run_id: str, level: str, message: str) -> None:
  async with session_scope() as db:
    db.add(m.ExecutionLog(id=new_id(), test_run_id=run_id, level=level, message=message))


# ---------------------------------------------------------------------------
# Running one test case
# ---------------------------------------------------------------------------


async def _load_environment(environment_id: Optional[str]) -> Optional[_ExecEnvironment]:
  if not environment_id:
    return None
  async with session_scope() as db:
    env = await db.get(m.Environment, environment_id)
  if env is None:
    return None
  credentials = json.loads(decrypt_secret(env.credentials_encrypted)) if env.credentials_encrypted else {}
  return _ExecEnvironment(
    base_url=env.base_url,
    config=BrowserConfig(
      browser=env.browser, headless=env.headless, viewport_width=env.viewport_width, viewport_height=env.viewport_height
    ),
    credentials=credentials,
  )


async def _inherited_state(project_id: str, options: ExecuteOptions) -> tuple[Optional[dict[str, Any]], Optional[str]]:
  """What to seed the fresh session with: the state saved after step N-1 of
  the run being resumed, or (opt-in) the user's live chat browser."""
  if options.resume_from:
    run_id, step_order = options.resume_from
    async with session_scope() as db:
      source = (
        await db.execute(
          select(m.TestRunResult).where(
            m.TestRunResult.test_run_id == run_id,
            m.TestRunResult.step_order == step_order - 1,
            m.TestRunResult.status == "passed",
          )
        )
      ).scalar_one_or_none()
    if source is None:
      return None, None
    return (json.loads(source.storage_state_json) if source.storage_state_json else None), source.page_url
  if options.continue_from_chat_session and options.user_id:
    key = chat.chat_session_key(project_id, options.user_id)
    browsers = services.get().browsers
    if browsers.has_session(key):
      return await browsers.capture_state(key)
  return None, None


async def execute_test_case(
  test_case_id: str,
  triggered_by: str,
  emit: Optional[Emit] = None,
  options: Optional[ExecuteOptions] = None,
  run_id: Optional[str] = None,
) -> dict[str, Any]:
  """Runs a test case now, in this process. `run_id` executes an existing
  pending run (queued by `start_test_case`) instead of creating one."""
  options = options or ExecuteOptions()
  emit = emit or (lambda _event: None)
  async with session_scope() as db:
    test_case = await db.get(m.TestCase, test_case_id)
    if test_case is None:
      raise LookupError("Test case not found")
    steps = list(
      (await db.execute(select(m.TestStep).where(m.TestStep.test_case_id == test_case_id).order_by(m.TestStep.order))).scalars()
    )
    url = await db.get(m.TargetUrl, test_case.url_id) if test_case.url_id else None
    project_id, title = test_case.project_id, test_case.title

  environment = await _load_environment(options.environment_id)
  async with session_scope() as db:
    if run_id:
      run = await db.get(m.TestRun, run_id)
      if run is None:
        raise LookupError("Test run not found")
      run.status, run.started_at = "running", utcnow()
    else:
      run = m.TestRun(
        id=new_id(),
        test_case_id=test_case_id,
        status="running",
        triggered_by=triggered_by,
        environment_id=options.environment_id,
        batch_id=options.batch_id,
        started_at=utcnow(),
      )
      db.add(run)
  run_id, batch_id = run.id, run.batch_id

  browsers = services.get().browsers
  session_key = f"run:{run_id}"
  browser = await browsers.get(session_key, environment.config if environment else None)
  await _start_tracing(browser)
  await _log(run_id, "info", f'Starting run for "{title}" ({triggered_by}).')

  overall = "passed"
  fallback_url = url.url if url else (environment.base_url if environment else None)
  try:
    state, state_url = await _inherited_state(project_id, options)
    if state:
      await _restore_storage_state(browser, state)
      if state_url:
        try:
          await _call_checked(browser, "browser_navigate", {"url": state_url})
        except Exception as err:  # noqa: BLE001
          logger.warning("failed to navigate resumed session to inherited URL: %s", err)

    to_run = [s for s in steps if s.order >= options.resume_from[1]] if options.resume_from else steps
    credentials = environment.credentials if environment else {}
    row_values = options.data_row.values if options.data_row else {}
    secret_columns = set(options.data_row.secret_columns) if options.data_row else set()

    for step in to_run:
      started = time.monotonic()
      async with session_scope() as db:
        result = m.TestRunResult(
          id=new_id(), test_run_id=run_id, test_step_id=step.id, step_order=step.order, action=step.action, status="running", duration_ms=0
        )
        db.add(result)
        if not step.enabled:
          result.status, result.actual_result = "skipped", "Step is disabled."
      if not step.enabled:
        emit({"type": "run.progress", "testRunId": run_id, "stepOrder": step.order, "status": "skipped", "batchId": batch_id})
        continue

      emit({"type": "run.progress", "testRunId": run_id, "stepOrder": step.order, "status": "running", "batchId": batch_id})
      try:
        outcome = await _run_step(browser, step, result.id, fallback_url, project_id, credentials, row_values, secret_columns)
        duration = int((time.monotonic() - started) * 1000)
        # Best-effort: lets a later "rerun from step N" start from here.
        state_after, page_url = await browsers.capture_state(session_key)
        async with session_scope() as db:
          row = await db.get(m.TestRunResult, result.id)
          assert row is not None
          row.status, row.actual_result, row.recovered, row.duration_ms = "passed", outcome.text, outcome.recovered, duration
          row.storage_state_json = json.dumps(state_after) if state_after else None
          row.page_url = page_url
        shot = await _capture_screenshot(browser, result.id, step.order)

        if outcome.recovered and outcome.resolved and outcome.resolved.matched_selector and step.selector:
          try:
            async with session_scope() as db:
              db.add(
                m.HealingEvent(
                  id=new_id(),
                  test_step_id=step.id,
                  test_run_result_id=result.id,
                  old_selector=step.selector,
                  new_selector=json.dumps(outcome.resolved.matched_selector),
                  confidence=outcome.resolved.confidence or "low",
                  note=outcome.resolved.note or "Selector automatically recovered.",
                  screenshot_id=shot,
                )
              )
          except Exception as err:  # noqa: BLE001
            logger.warning("failed to record healing event: %s", err)

        await _log(run_id, "info", f"Step {step.order} ({step.action}) passed: {step.description}")
        emit({"type": "run.progress", "testRunId": run_id, "stepOrder": step.order, "status": "passed", "batchId": batch_id})
      except Exception as err:  # noqa: BLE001 -- any step error fails the run
        duration = int((time.monotonic() - started) * 1000)
        message = str(err) or err.__class__.__name__
        await _capture_screenshot(browser, result.id, step.order)
        error_text, suggested_fix = message, "Review the step manually."
        try:
          explanation = await chat.explain_failure(project_id, step.description, step.action, message, await _safe_snapshot(browser))
          error_text = f"{message}\n\nWhy it likely failed: {explanation.explanation}"
          suggested_fix = explanation.suggestedFix
        except Exception as explain_err:  # noqa: BLE001
          logger.warning("failure explanation call failed: %s", explain_err)
        async with session_scope() as db:
          row = await db.get(m.TestRunResult, result.id)
          assert row is not None
          row.status, row.error_message, row.suggested_fix, row.duration_ms = "failed", error_text, suggested_fix, duration
        await _log(run_id, "error", f"Step {step.order} ({step.action}) failed: {message}")
        emit(
          {
            "type": "run.progress",
            "testRunId": run_id,
            "stepOrder": step.order,
            "status": "failed",
            "message": message,
            "batchId": batch_id,
          }
        )
        overall = "failed"
        break
  finally:
    trace_path, video_path = await _stop_tracing(browser)
    await _capture_diagnostics(browser, run_id)
    await browsers.close(session_key)

  async with session_scope() as db:
    row_run = await db.get(m.TestRun, run_id)
    assert row_run is not None
    row_run.status = overall
    row_run.finished_at = utcnow()
    row_run.trace_path, row_run.video_path = trace_path, video_path
    row_run.continued_from_chat = bool(options.continue_from_chat_session)
    row_run.resumed_from_run_id = options.resume_from[0] if options.resume_from else None
    row_run.resumed_from_step_order = options.resume_from[1] if options.resume_from else None
    row_run.data_row_index = options.data_row.index if options.data_row else None
    if overall != "passed":
      remember(db, "run_result", run_id)
  await _log(run_id, "info", f"Run finished: {overall}.")

  async with session_scope() as db:
    record = await run_records.record(db, run_id)
  assert record is not None
  emit({"type": "run.completed", "testRun": record})
  if overall != "passed":
    await send_failure_webhook(record, title)
  return record


async def _run_step(
  browser: BrowserToolset,
  step: m.TestStep,
  result_id: str,
  fallback_url: Optional[str],
  project_id: str,
  credentials: dict[str, str],
  row: dict[str, str],
  secret_columns: set[str],
) -> _StepOutcome:
  selector: Optional[dict[str, Any]] = json.loads(step.selector) if step.selector else None
  candidates: Optional[list[dict[str, Any]]] = json.loads(step.locator_candidates) if step.locator_candidates else None
  value = substitute(step.value, credentials, row)
  display = masked_display_value(step.value, row, secret_columns)
  action = step.action

  def require_selector() -> dict[str, Any]:
    if not selector:
      raise StepFailure(f'Step "{step.description}" requires a selector but none was recorded.')
    return selector

  async def resolve() -> tuple[dict[str, Any], Resolved]:
    el = require_selector()
    return el, await resolve_element(browser, el, project_id, candidates)

  if action == "navigate":
    target = value or fallback_url
    if not target:
      raise StepFailure("navigate step has no URL and the test case has no target URL")
    await _call_checked(browser, "browser_navigate", {"url": target})
    return _StepOutcome(f"Navigated to {target}")

  if action in ("click", "check", "uncheck"):
    el, resolved = await resolve()
    await _call_checked(browser, "browser_click", {"element": el["description"], "target": resolved.ref})
    return _StepOutcome(f"Clicked {el['description']}", resolved.recovered, resolved)

  if action == "hover":
    el, resolved = await resolve()
    await _call_checked(browser, "browser_hover", {"element": el["description"], "target": resolved.ref})
    return _StepOutcome(f"Hovered {el['description']}", resolved.recovered, resolved)

  if action == "fill":
    el, resolved = await resolve()
    await _call_checked(browser, "browser_type", {"element": el["description"], "target": resolved.ref, "text": value or ""})
    return _StepOutcome(f'Filled {el["description"]} with "{display}"', resolved.recovered, resolved)

  if action == "select":
    el, resolved = await resolve()
    await _call_checked(
      browser, "browser_select_option", {"element": el["description"], "target": resolved.ref, "values": [value or ""]}
    )
    return _StepOutcome(f'Selected "{display}" in {el["description"]}', resolved.recovered, resolved)

  if action == "press":
    key = value if value is not None else "Enter"
    await _call_checked(browser, "browser_press_key", {"key": key})
    return _StepOutcome(f'Pressed key "{key}"')

  if action == "waitFor":
    # A number waits that long; anything else waits for that text to appear.
    try:
      seconds: Optional[float] = float(value) if value else None
    except ValueError:
      seconds = None
    if seconds is not None and seconds == seconds:  # not NaN
      await _call_checked(browser, "browser_wait_for", {"time": seconds})
    else:
      await _call_checked(browser, "browser_wait_for", {"text": value or ""})
    return _StepOutcome(f"Waited for {value if value is not None else 'timeout'}")

  if action == "assertVisible":
    el, resolved = await resolve()
    return _StepOutcome(f"Verified visible: {el['description']}", resolved.recovered, resolved)

  if action == "assertText":
    expected = value or ""
    deadline = time.monotonic() + ASSERTION_POLL_TIMEOUT
    while True:
      if expected.lower() in (await _safe_snapshot(browser)).lower():
        return _StepOutcome(f'Verified page contains text "{expected}"')
      await asyncio.sleep(ASSERTION_POLL_INTERVAL)
      if time.monotonic() >= deadline:
        raise StepFailure(f'Expected page to contain text "{expected}" but it was not found.')

  if action == "assertUrl":
    expected = value or ""
    current: Optional[str] = None
    deadline = time.monotonic() + ASSERTION_POLL_TIMEOUT
    while True:
      current = extract_url_from_snapshot(await _safe_snapshot(browser))
      if current and expected in current:
        return _StepOutcome(f'Verified URL contains "{expected}"')
      await asyncio.sleep(ASSERTION_POLL_INTERVAL)
      if time.monotonic() >= deadline:
        raise StepFailure(f'Expected URL to contain "{expected}" but current URL is "{current or "unknown"}".')

  if action == "screenshot":
    # Every step is screenshotted already; this marks an explicit checkpoint.
    return _StepOutcome("Captured screenshot")

  if action in ("assertEnabled", "assertDisabled"):
    el, resolved = await resolve()
    raw = await _evaluate_on(
      browser,
      el["description"],
      resolved.ref,
      "(element) => String(!!(element.disabled || element.getAttribute('aria-disabled') === 'true'))",
    )
    is_disabled = "true" in raw.strip().lower()
    expect_disabled = action == "assertDisabled"
    if is_disabled != expect_disabled:
      raise StepFailure(
        f"Expected {el['description']} to be {'disabled' if expect_disabled else 'enabled'}, "
        f"but it was {'disabled' if is_disabled else 'enabled'}."
      )
    return _StepOutcome(f"Verified {el['description']} is {'disabled' if expect_disabled else 'enabled'}", resolved.recovered, resolved)

  if action == "assertTableContains":
    el = require_selector()
    expected = value or ""
    deadline = time.monotonic() + ASSERTION_POLL_TIMEOUT
    while True:
      resolved = await resolve_element(browser, el, project_id, candidates)
      text = await _evaluate_on(browser, el["description"], resolved.ref, "(element) => element.innerText || element.textContent || ''")
      if expected.lower() in text.lower():
        return _StepOutcome(f'Verified {el["description"]} contains "{expected}"', resolved.recovered, resolved)
      await asyncio.sleep(ASSERTION_POLL_INTERVAL)
      if time.monotonic() >= deadline:
        raise StepFailure(f'Expected {el["description"]} to contain "{expected}" but found: "{text[:200]}"')

  if action == "assertApiResponse":
    try:
      criteria = json.loads(value or "")
      if not isinstance(criteria.get("urlPattern"), str) or not isinstance(criteria.get("expectedStatus"), (int, float)):
        raise ValueError
    except (ValueError, AttributeError):
      raise StepFailure(
        'assertApiResponse requires a value like {"urlPattern": "/api/login", "expectedStatus": 200} '
        f"but got: {value or '(empty)'}"
      ) from None
    text = mcp_result_to_text(await _call_checked(browser, "browser_network_requests", {}))
    matching = [line for line in text.split("\n") if criteria["urlPattern"] in line]
    if not matching:
      raise StepFailure(
        f'No network request matching "{criteria["urlPattern"]}" was found. This may mean the request hasn\'t '
        f"happened yet, or that browser_network_requests' output format has changed -- captured requests:\n{text[:1000]}"
      )
    statuses = [re.search(r"\b([1-5]\d{2})\b", line) for line in matching]
    if not any(s and int(s.group(1)) == criteria["expectedStatus"] for s in statuses):
      raise StepFailure(
        f'Found request(s) matching "{criteria["urlPattern"]}" but none had status {criteria["expectedStatus"]}:\n'
        + "\n".join(matching)
      )
    return _StepOutcome(f'Verified request matching "{criteria["urlPattern"]}" returned status {criteria["expectedStatus"]}')

  if action == "assertFormValid":
    el, resolved = await resolve()
    raw = await _evaluate_on(
      browser,
      el["description"],
      resolved.ref,
      "(element) => JSON.stringify({ valid: typeof element.checkValidity === 'function' ? element.checkValidity() : true, message: element.validationMessage || '' })",
    )
    try:
      state = json.loads(raw)
    except ValueError:
      raise StepFailure(f"Could not read validity state for {el['description']}: {raw}") from None
    expected_raw, _, rest = (value or "valid").partition(":")
    expected_valid = expected_raw.strip().lower() != "invalid"
    expected_message = rest.strip()
    if state.get("valid") != expected_valid:
      raise StepFailure(
        f"Expected {el['description']} to be {'valid' if expected_valid else 'invalid'}, but checkValidity() returned "
        f"{str(state.get('valid')).lower()} (message: \"{state.get('message', '')}\")."
      )
    if expected_message and expected_message.lower() not in str(state.get("message", "")).lower():
      raise StepFailure(f'Expected validation message to contain "{expected_message}" but got "{state.get("message", "")}".')
    return _StepOutcome(
      f"Verified form validity of {el['description']}: {'valid' if expected_valid else 'invalid'}", resolved.recovered, resolved
    )

  if action == "assertAccessible":
    el, resolved = await resolve()
    raw = await _evaluate_on(browser, el["description"], resolved.ref, ACCESSIBLE_NAME_CHECK_FN)
    if "true" not in raw.strip().lower():
      raise StepFailure(
        f"{el['description']} does not have an accessible name (missing alt text, label, or aria-label). "
        "Note: this is a lightweight heuristic, not a full accessibility audit."
      )
    return _StepOutcome(f"Verified {el['description']} has an accessible name", resolved.recovered, resolved)

  if action == "assertVisualMatch":
    shot = await browser.call_tool("browser_take_screenshot", {"type": "png"})
    if mcp_result_is_error(shot):
      raise StepFailure(mcp_result_to_text(shot) or "Failed to capture a screenshot for the visual check.")
    data = _image_bytes(shot)
    if data is None:
      raise StepFailure("browser_take_screenshot returned no image data for the visual check.")
    try:
      threshold = float(value) if value else None
    except ValueError:
      threshold = None
    async with session_scope() as db:
      outcome = await check_visual_match(db, step.id, result_id, data, threshold)
    if not outcome.passed:
      raise StepFailure(outcome.note)
    return _StepOutcome(outcome.note)

  raise StepFailure(f"Unknown step action: {action}")


# ---------------------------------------------------------------------------
# Reruns and batches
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Starting runs: in this process (TASK_QUEUE=inline) or on a worker via
# Pub/Sub (TASK_QUEUE=pubsub, see aegisqa/tasks.py and aegisqa/worker.py).
# ---------------------------------------------------------------------------


def options_to_json(options: ExecuteOptions) -> dict[str, Any]:
  return {
    "environmentId": options.environment_id,
    "batchId": options.batch_id,
    "continueFromChatSession": options.continue_from_chat_session,
    "userId": options.user_id,
    "resumeFrom": list(options.resume_from) if options.resume_from else None,
    "dataRow": (
      {"index": options.data_row.index, "values": options.data_row.values, "secretColumns": options.data_row.secret_columns}
      if options.data_row
      else None
    ),
  }


def options_from_json(data: dict[str, Any]) -> ExecuteOptions:
  row = data.get("dataRow")
  resume = data.get("resumeFrom")
  return ExecuteOptions(
    environment_id=data.get("environmentId"),
    batch_id=data.get("batchId"),
    continue_from_chat_session=bool(data.get("continueFromChatSession")),
    user_id=data.get("userId"),
    resume_from=(str(resume[0]), int(resume[1])) if resume else None,
    data_row=DataRow(int(row["index"]), dict(row["values"]), list(row["secretColumns"])) if row else None,
  )


async def _queue_run(test_case_id: str, triggered_by: str, options: ExecuteOptions) -> dict[str, Any]:
  """Creates a pending run and hands it to the workers."""
  async with session_scope() as db:
    if await db.get(m.TestCase, test_case_id) is None:
      raise LookupError("Test case not found")
    run = m.TestRun(
      id=new_id(),
      test_case_id=test_case_id,
      status="pending",
      triggered_by=triggered_by,
      environment_id=options.environment_id,
      batch_id=options.batch_id,
      started_at=utcnow(),
    )
    db.add(run)
  svc = services.get()
  assert svc.publisher is not None
  await svc.publisher.publish(
    settings.pubsub_runs_topic,
    {"testRunId": run.id, "testCaseId": test_case_id, "triggeredBy": triggered_by, "options": options_to_json(options)},
  )
  async with session_scope() as db:
    record = await run_records.record(db, run.id)
  assert record is not None
  return record


async def start_test_case(
  test_case_id: str, triggered_by: str, emit: Optional[Emit] = None, options: Optional[ExecuteOptions] = None
) -> dict[str, Any]:
  """Inline: runs now and returns the finished run. Queued: returns the
  pending run straight away; progress arrives over the WebSocket."""
  options = options or ExecuteOptions()
  if services.get().publisher is not None:
    return await _queue_run(test_case_id, triggered_by, options)
  return await execute_test_case(test_case_id, triggered_by, emit, options)


async def rerun(run_id: str, emit: Optional[Emit] = None, resume_from_step_order: Optional[int] = None) -> dict[str, Any]:
  async with session_scope() as db:
    previous = await db.get(m.TestRun, run_id)
    if previous is None:
      raise LookupError("Test run not found")
    test_case_id, environment_id = previous.test_case_id, previous.environment_id
  return await start_test_case(
    test_case_id,
    "rerun",
    emit,
    ExecuteOptions(
      environment_id=environment_id,
      resume_from=(run_id, resume_from_step_order) if resume_from_step_order else None,
    ),
  )


async def _run_many(jobs: list[tuple[str, ExecuteOptions]], triggered_by: str, emit: Optional[Emit]) -> list[str]:
  """Runs (inline: MAX_PARALLEL_RUNS at a time) or queues (all at once) a batch."""
  if services.get().publisher is not None:
    return [(await _queue_run(tc_id, triggered_by, opts))["id"] for tc_id, opts in jobs]
  run_ids: list[str] = []
  size = max(1, settings.max_parallel_runs)
  for start in range(0, len(jobs), size):
    results = await asyncio.gather(
      *(execute_test_case(tc_id, triggered_by, emit, opts) for tc_id, opts in jobs[start : start + size]),
      return_exceptions=True,
    )
    for result in results:
      if isinstance(result, BaseException):
        logger.error("batch run failed: %s", result)
      else:
        run_ids.append(result["id"])
  return run_ids


async def run_approved_batch(
  project_id: str,
  environment_id: Optional[str],
  emit: Optional[Emit] = None,
  continue_from_chat_session: bool = False,
  user_id: Optional[str] = None,
) -> dict[str, Any]:
  """Every approved test case in the project, grouped under one batch id."""
  batch_id = new_batch_id()
  async with session_scope() as db:
    ids = list(
      (
        await db.execute(
          select(m.TestCase.id).where(m.TestCase.project_id == project_id, m.TestCase.status == "approved").order_by(m.TestCase.created_at)
        )
      ).scalars()
    )
  options = ExecuteOptions(
    environment_id=environment_id, batch_id=batch_id, continue_from_chat_session=continue_from_chat_session, user_id=user_id
  )
  return {"batchId": batch_id, "testRunIds": await _run_many([(tc_id, options) for tc_id in ids], "batch", emit)}


async def run_across_data_set(test_case_id: str, environment_id: Optional[str], emit: Optional[Emit] = None) -> dict[str, Any]:
  """One run per row of the test case's data set, grouped under one batch id."""
  async with session_scope() as db:
    data_set = (await db.execute(select(m.TestDataSet).where(m.TestDataSet.test_case_id == test_case_id))).scalar_one_or_none()
  if data_set is None:
    raise LookupError("This test case has no test data set to run with.")
  rows = json.loads(data_set.rows)
  secret_columns = json.loads(data_set.secret_columns)
  batch_id = new_batch_id()
  jobs = [
    (test_case_id, ExecuteOptions(environment_id=environment_id, batch_id=batch_id, data_row=DataRow(index, values, secret_columns)))
    for index, values in enumerate(rows)
  ]
  return {"batchId": batch_id, "testRunIds": await _run_many(jobs, "batch", emit)}


async def recover_interrupted_runs() -> None:
  """Runs that can no longer finish become `error`: with inline execution,
  anything still running when the server (re)starts; with queued execution,
  runs stuck for longer than a run can take."""
  from datetime import timedelta

  async with session_scope() as db:
    query = select(m.TestRun).where(m.TestRun.status.in_(["pending", "running"]))
    if services.get().publisher is not None:
      query = query.where(m.TestRun.started_at < utcnow() - timedelta(hours=3))
    for run in (await db.execute(query)).scalars():
      run.status, run.finished_at = "error", utcnow()
      db.add(m.ExecutionLog(id=new_id(), test_run_id=run.id, level="error", message="Run interrupted (server restart or lost worker)."))


# ---------------------------------------------------------------------------
# Failure webhook (Slack-compatible) -- best-effort, never fails the run.
# ---------------------------------------------------------------------------


async def send_failure_webhook(run: dict[str, Any], test_case_title: str) -> None:
  if not settings.webhook_url:
    return
  failed = next((r for r in run["results"] if r["status"] in ("failed", "error")), None)
  short_id = run["id"][-8:]
  text = (
    f'Test run failed: "{test_case_title}" (run {short_id}) — step {failed["stepOrder"]}: '
    f'{failed["errorMessage"] or "no error message"}'
    if failed
    else f'Test run {run["status"]}: "{test_case_title}" (run {short_id})'
  )
  payload = {
    "text": text,
    "testRunId": run["id"],
    "testCaseTitle": test_case_title,
    "status": run["status"],
    "failedStepOrder": failed["stepOrder"] if failed else None,
    "errorMessage": failed["errorMessage"] if failed else None,
  }
  try:
    async with httpx.AsyncClient(timeout=15) as client:
      response = await client.post(settings.webhook_url, json=payload)
    if response.status_code >= 400:
      logger.warning("failure webhook returned %s", response.status_code)
  except Exception as err:  # noqa: BLE001
    logger.warning("failed to deliver failure webhook: %s", err)
