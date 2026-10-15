"""The continuous learning loop: whenever something worth remembering changes,
it's (re)written to memory --

  test case created / edited / approved   -> test_case
  test run failed                         -> run_result   (error code + suggested fix)
  self-heal approved                      -> healing      (old -> new locator for an element)
  defect filed in Azure DevOps            -> defect

Call `remember(db, kind, ref_id)` (or `forget`) inside the transaction that
made the change. The memory write runs only after that transaction commits,
in the background, so it never slows down or breaks the request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Optional

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from ..db import models as m
from ..db.base import session_scope
from .service import MemoryItem

logger = logging.getLogger(__name__)

_PENDING = "memory_pending"
_tasks: set[asyncio.Task[None]] = set()


def remember(db: AsyncSession, kind: str, ref_id: str) -> None:
  db.sync_session.info.setdefault(_PENDING, []).append(("index", kind, ref_id, None))


def forget(db: AsyncSession, project_id: str, kind: str, ref_id: str) -> None:
  db.sync_session.info.setdefault(_PENDING, []).append(("remove", kind, ref_id, project_id))


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
  pending = session.info.pop(_PENDING, None)
  if not pending:
    return
  try:
    loop = asyncio.get_running_loop()
  except RuntimeError:
    return
  for job in pending:
    task = loop.create_task(_run(*job))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
  session.info.pop(_PENDING, None)


async def _run(action: str, kind: str, ref_id: str, project_id: Optional[str]) -> None:
  from .. import services

  try:
    memory = services.get().memory
  except RuntimeError:
    return  # no app (scripts, CLI)
  if memory is None:
    return
  try:
    if action == "remove":
      assert project_id is not None
      await memory.remove(project_id, kind, ref_id)
      return
    item = await build_item(kind, ref_id)
    if item is not None:
      await memory.index(item)
  except Exception:  # noqa: BLE001 -- learning is best effort
    logger.exception("memory: failed to %s %s %s", action, kind, ref_id)


async def wait_for_idle() -> None:
  """Test helper: wait for pending memory writes."""
  while _tasks:
    await asyncio.gather(*list(_tasks), return_exceptions=True)


# ---------------------------------------------------------------------------
# What gets remembered about each kind of item
# ---------------------------------------------------------------------------


def classify_error(message: str) -> str:
  """A coarse, stable error code for exact lookups ("have we seen this before?")."""
  lowered = message.lower()
  if match := re.search(r"net::(err_[a-z_]+)", lowered):
    return match.group(1).upper()
  if "could not locate element" in lowered or "requires a selector" in lowered:
    return "ELEMENT_NOT_FOUND"
  if match := re.search(r"none had status (\d{3})|returned status (\d{3})", lowered):
    return f"HTTP_{match.group(1) or match.group(2)}"
  if "timeout" in lowered or "timed out" in lowered:
    return "TIMEOUT"
  if lowered.startswith("expected") or "but it was" in lowered or "visual difference" in lowered:
    return "ASSERTION_FAILED"
  return "STEP_FAILED"


async def build_item(kind: str, ref_id: str) -> Optional[MemoryItem]:
  async with session_scope() as db:
    builder = _BUILDERS[kind]
    return await builder(db, ref_id)


async def _test_case(db: AsyncSession, ref_id: str) -> Optional[MemoryItem]:
  tc = await db.get(m.TestCase, ref_id)
  if tc is None:
    return None
  steps = (await db.execute(select(m.TestStep).where(m.TestStep.test_case_id == ref_id).order_by(m.TestStep.order))).scalars()
  step_lines = []
  for st in steps:
    target = ""
    if st.selector:
      sel = json.loads(st.selector)
      target = f" [{sel.get('role') or sel.get('strategy')}: {sel.get('name') or sel.get('label') or sel.get('description')}]"
    step_lines.append(f"{st.order}. {st.action}{target} -- {st.description}")
  return MemoryItem(
    project_id=tc.project_id,
    kind="test_case",
    ref_id=tc.id,
    title=tc.title,
    content=(
      f"Objective: {tc.objective}\nPreconditions: {tc.preconditions}\nExpected: {tc.expected_result}\n"
      f"Type: {tc.type}, priority {tc.priority}\nSteps:\n" + "\n".join(step_lines)
    ),
    module=tc.module,
    metadata={"status": tc.status, "priority": tc.priority, "type": tc.type, "tags": json.loads(tc.tags or "[]")},
  )


async def _run_result(db: AsyncSession, ref_id: str) -> Optional[MemoryItem]:
  run = await db.get(m.TestRun, ref_id)
  if run is None or run.status not in ("failed", "error"):
    return None
  tc = await db.get(m.TestCase, run.test_case_id)
  failed = (
    await db.execute(
      select(m.TestRunResult, m.TestStep.description)
      .join(m.TestStep, m.TestStep.id == m.TestRunResult.test_step_id)
      .where(m.TestRunResult.test_run_id == ref_id, m.TestRunResult.status.in_(["failed", "error"]))
      .order_by(m.TestRunResult.step_order)
      .limit(1)
    )
  ).first()
  if tc is None or failed is None:
    return None
  result, step_description = failed
  error = (result.error_message or "").split("\n\nWhy it likely failed:")[0]
  return MemoryItem(
    project_id=tc.project_id,
    kind="run_result",
    ref_id=run.id,
    title=f"Failed: {tc.title} (step {result.step_order})",
    content=(
      f"Test case: {tc.title}\nFailed step {result.step_order} ({result.action}): {step_description}\n"
      f"Error: {result.error_message or ''}\nSuggested fix: {result.suggested_fix or ''}\nPage: {result.page_url or ''}"
    ),
    module=tc.module,
    error_code=classify_error(error),
    metadata={"testCaseId": tc.id, "status": run.status, "stepOrder": result.step_order, "finishedAt": str(run.finished_at)},
  )


async def _healing(db: AsyncSession, ref_id: str) -> Optional[MemoryItem]:
  event_row = await db.get(m.HealingEvent, ref_id)
  if event_row is None or not event_row.approved:
    return None
  step = await db.get(m.TestStep, event_row.test_step_id)
  tc = await db.get(m.TestCase, step.test_case_id) if step else None
  if step is None or tc is None:
    return None
  old, new = json.loads(event_row.old_selector), json.loads(event_row.new_selector)
  return MemoryItem(
    project_id=tc.project_id,
    kind="healing",
    ref_id=event_row.id,
    title=f"Element moved: {old.get('description') or step.description}",
    content=(
      f"Element: {old.get('description')}\nOld locator: {json.dumps(old)}\nNew locator: {json.dumps(new)}\n"
      f"In test case: {tc.title}, step {step.order} -- {step.description}\nNote: {event_row.note}"
    ),
    module=tc.module,
    metadata={"testCaseId": tc.id, "testStepId": step.id, "confidence": event_row.confidence},
  )


async def _defect(db: AsyncSession, ref_id: str) -> Optional[MemoryItem]:
  push = await db.get(m.IssuePush, ref_id)
  if push is None or push.status != "pushed":
    return None
  run = await db.get(m.TestRun, push.test_run_id)
  tc = await db.get(m.TestCase, run.test_case_id) if run else None
  if tc is None:
    return None
  errors = (
    await db.execute(
      select(m.TestRunResult.error_message).where(
        m.TestRunResult.test_run_id == push.test_run_id, m.TestRunResult.error_message.is_not(None)
      )
    )
  ).scalars()
  error_text = "\n".join(e for e in errors if e)
  return MemoryItem(
    project_id=tc.project_id,
    kind="defect",
    ref_id=push.id,
    title=f"Defect {push.issue_key}: {tc.title}",
    content=f"Filed in {push.type} as {push.issue_key} ({push.issue_url})\nTest case: {tc.title}\n{error_text}",
    module=tc.module,
    error_code=classify_error(error_text.split("\n\nWhy it likely failed:")[0]) if error_text else None,
    metadata={"issueKey": push.issue_key, "issueUrl": push.issue_url, "testCaseId": tc.id, "testRunId": push.test_run_id},
  )


_BUILDERS: dict[str, Any] = {
  "test_case": _test_case,
  "run_result": _run_result,
  "healing": _healing,
  "defect": _defect,
}


async def backfill(project_id: Optional[str] = None, kinds: tuple[str, ...] = ("test_case",)) -> int:
  """Indexes items that have no memory record yet. Returns how many."""
  from .. import services

  memory = services.get().memory
  queries = {
    "test_case": select(m.TestCase.id, m.TestCase.project_id),
    "run_result": select(m.TestRun.id, m.TestCase.project_id)
    .join(m.TestCase, m.TestCase.id == m.TestRun.test_case_id)
    .where(m.TestRun.status.in_(["failed", "error"])),
    "healing": select(m.HealingEvent.id, m.TestCase.project_id)
    .join(m.TestStep, m.TestStep.id == m.HealingEvent.test_step_id)
    .join(m.TestCase, m.TestCase.id == m.TestStep.test_case_id)
    .where(m.HealingEvent.approved.is_(True)),
    "defect": select(m.IssuePush.id, m.TestCase.project_id)
    .join(m.TestRun, m.TestRun.id == m.IssuePush.test_run_id)
    .join(m.TestCase, m.TestCase.id == m.TestRun.test_case_id)
    .where(m.IssuePush.status == "pushed"),
  }
  count = 0
  for kind in kinds:
    async with session_scope() as db:
      query = queries[kind]
      if project_id:
        query = query.where(m.TestCase.project_id == project_id)
      rows = (await db.execute(query)).all()
      known = set(
        (await db.execute(select(m.MemoryRecord.ref_id).where(m.MemoryRecord.kind == kind))).scalars()
      )
    for ref_id, _ in rows:
      if ref_id in known:
        continue
      item = await build_item(kind, ref_id)
      if item is not None:
        await memory.index(item)
        count += 1
  return count
