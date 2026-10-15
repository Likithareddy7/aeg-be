"""Resolving a stored, resilient selector to a live element ref.

Order: the primary selector -> previously-approved alternates
(TestStep.locatorCandidates) -> a raw CSS/XPath check -> asking the
self-healing agent as a last resort. Anything past the primary selector is
reported as "recovered" so a HealingEvent can be queued for human review.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from .. import chat
from ..agents.browser import BrowserToolset, mcp_result_is_error, mcp_result_to_text

logger = logging.getLogger(__name__)

# Playwright MCP snapshot lines look like `- button "Log in" [ref=e12]`, with
# optional state annotations before the ref (`- textbox "User" [active] [ref=e3]`).
_SNAPSHOT_LINE = re.compile(r'-\s*([a-zA-Z][\w-]*)\s*(?:"([^"]*)")?\s*(?:\[(?!ref=)[^\]]*\]\s*)*\[ref=([^\]\s]+)\]')


@dataclass
class Resolved:
  ref: str
  recovered: bool
  snapshot_text: str
  matched_selector: Optional[dict[str, Any]] = None
  confidence: Optional[str] = None
  note: Optional[str] = None


@dataclass
class _Entry:
  role: str
  name: Optional[str]
  ref: str


def _parse(text: str) -> list[_Entry]:
  return [_Entry(role=m.group(1), name=m.group(2), ref=m.group(3)) for m in _SNAPSHOT_LINE.finditer(text)]


def _direct_match(entries: list[_Entry], selector: dict[str, Any]) -> Optional[_Entry]:
  role = (selector.get("role") or "").lower() or None
  name_source = next(
    (selector.get(k) for k in ("name", "label", "placeholder", "altText", "testId") if selector.get(k) is not None), None
  )
  name = name_source.lower() if name_source else None
  if role:
    for entry in entries:
      if entry.role.lower() == role and name and (entry.name or "").lower() == name:
        return entry
    for entry in entries:
      if entry.role.lower() == role and name and name in (entry.name or "").lower():
        return entry
  if name:
    for entry in entries:
      if (entry.name or "").lower() == name:
        return entry
  return None


async def _raw_selector(browser: BrowserToolset, selector: dict[str, Any]) -> Optional[str]:
  """CSS/XPath never appear in the accessibility tree, so check them against
  the live DOM and pass them through as the target."""
  strategy = selector.get("strategy")
  if strategy == "css" and selector.get("css"):
    return selector["css"] if await _exists(browser, selector["css"], xpath=False) else None
  if strategy == "xpath" and selector.get("xpath"):
    return f"xpath={selector['xpath']}" if await _exists(browser, selector["xpath"], xpath=True) else None
  return None


async def _exists(browser: BrowserToolset, expression: str, *, xpath: bool) -> bool:
  quoted = json.dumps(expression)
  fn = (
    f"() => !!document.evaluate({quoted}, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue"
    if xpath
    else f"() => !!document.querySelector({quoted})"
  )
  try:
    result = await browser.call_tool("browser_evaluate", {"function": fn})
  except Exception:  # noqa: BLE001
    return False
  return not mcp_result_is_error(result) and "true" in mcp_result_to_text(result).lower()


async def resolve_element(
  browser: BrowserToolset,
  selector: dict[str, Any],
  project_id: str,
  candidates: Optional[list[dict[str, Any]]] = None,
) -> Resolved:
  snapshot = await browser.call_tool("browser_snapshot")
  snapshot_text = mcp_result_to_text(snapshot)
  if mcp_result_is_error(snapshot):
    raise RuntimeError(snapshot_text or "browser_snapshot reported an error.")
  entries = _parse(snapshot_text)

  direct = _direct_match(entries, selector)
  if direct:
    return Resolved(ref=direct.ref, recovered=False, snapshot_text=snapshot_text)
  raw = await _raw_selector(browser, selector)
  if raw:
    return Resolved(ref=raw, recovered=False, snapshot_text=snapshot_text)

  for candidate in candidates or []:
    match = _direct_match(entries, candidate)
    ref = match.ref if match else await _raw_selector(browser, candidate)
    if ref:
      return Resolved(
        ref=ref,
        recovered=True,
        snapshot_text=snapshot_text,
        matched_selector=candidate,
        confidence="high",
        note="Matched a previously-approved alternate locator.",
      )

  logger.warning('no direct match for "%s", asking the self-healing agent', selector.get("description"))
  try:
    recovery = await chat.recover_selector(
      project_id, selector.get("role"), selector.get("name"), selector["description"], snapshot_text
    )
  except Exception as err:  # noqa: BLE001 -- no model available: report "not found", not a crash
    raise RuntimeError(
      f'Could not locate element "{selector["description"]}" on the page (self-healing unavailable: {err})'
    ) from err
  if not recovery.ref:
    raise RuntimeError(f'Could not locate element "{selector["description"]}" on the page ({recovery.note})')

  matched = None
  if recovery.matchedRole:
    matched = {"strategy": "role", "role": recovery.matchedRole, "description": selector["description"]}
    if recovery.matchedName:
      matched["name"] = recovery.matchedName
  return Resolved(
    ref=recovery.ref,
    recovered=True,
    snapshot_text=snapshot_text,
    matched_selector=matched,
    confidence=recovery.confidence,
    note=recovery.note,
  )


def extract_url_from_snapshot(snapshot_text: str) -> Optional[str]:
  match = re.search(r"https?://\S+", snapshot_text)
  return re.sub(r"[)\]\"'.,]+$", "", match.group(0)) if match else None
