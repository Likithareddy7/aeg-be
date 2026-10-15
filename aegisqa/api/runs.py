"""/api/testruns, /api/healing-events, /api/visual-diffs."""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from ..auth.sessions import CurrentUser, require_project_role
from ..db import models as m
from ..db import runs as run_records
from ..db import serializers as s
from ..db.base import iso, utcnow
from ..db.session import Db
from ..memory.learning import remember
from ..errors import ApiError, parse_body
from ..execution import executor
from ..execution.reports import BatchRun, html_report, junit_xml
from ..execution.visual import approve_diff
from ..integrations import azure_devops, defects
from ..ws.registry import project_emit
from .common import json_body

router = APIRouter()


async def _run_project(db: Db, user: CurrentUser, run: m.TestRun, min_role: str = "viewer") -> str:
  tc = await db.get(m.TestCase, run.test_case_id)
  assert tc is not None
  await require_project_role(db, user, tc.project_id, min_role)
  return tc.project_id


async def _run(db: Db, user: CurrentUser, run_id: str) -> m.TestRun:
  run = await db.get(m.TestRun, run_id)
  if run is None:
    raise ApiError(404, "Test run not found")
  await _run_project(db, user, run)
  return run


@router.get("/api/testruns/{run_id}")
async def get_run(run_id: str, user: CurrentUser, db: Db) -> dict[str, Any]:
  run = await _run(db, user, run_id)
  return (await run_records.records(db, [run], with_inherited=True))[0]


@router.get("/api/testruns/by-testcase/{test_case_id}")
async def runs_for_test_case(test_case_id: str, user: CurrentUser, db: Db) -> list[dict[str, Any]]:
  tc = await db.get(m.TestCase, test_case_id)
  if tc is None:
    return []
  await require_project_role(db, user, tc.project_id, "viewer")
  runs = list(
    (await db.execute(select(m.TestRun).where(m.TestRun.test_case_id == test_case_id).order_by(m.TestRun.started_at.desc()))).scalars()
  )
  return await run_records.records(db, runs)


@router.get("/api/testruns/{run_id}/trace")
async def trace(run_id: str, user: CurrentUser, db: Db) -> dict[str, Any]:
  run = await _run(db, user, run_id)
  record = (await run_records.records(db, [run]))[0]
  return {
    "testRun": record,
    "consoleLogs": [log for log in record["logs"] if log["category"] == "console"],
    "networkLogs": [log for log in record["logs"] if log["category"] == "network"],
  }


async def _batch(db: Db, user: CurrentUser, batch_id: str) -> list[BatchRun]:
  rows = (
    await db.execute(
      select(m.TestRun, m.TestCase.title, m.TestCase.project_id)
      .join(m.TestCase, m.TestCase.id == m.TestRun.test_case_id)
      .where(m.TestRun.batch_id == batch_id)
      .order_by(m.TestRun.started_at)
    )
  ).all()
  if not rows:
    raise ApiError(404, "No runs found for that batch id")
  await require_project_role(db, user, rows[0][2], "viewer")
  out = []
  for run, title, _ in rows:
    error = (
      await db.execute(
        select(m.TestRunResult.error_message)
        .where(m.TestRunResult.test_run_id == run.id, m.TestRunResult.error_message.is_not(None))
        .order_by(m.TestRunResult.step_order)
        .limit(1)
      )
    ).scalar_one_or_none()
    out.append(
      BatchRun(
        id=run.id,
        test_case_title=title,
        status=run.status,
        started_at=iso(run.started_at) or "",
        finished_at=iso(run.finished_at),
        duration_ms=s.run_duration_ms(run),
        error_message=error,
      )
    )
  return out


@router.get("/api/testruns/batch/{batch_id}/junit")
async def batch_junit(batch_id: str, user: CurrentUser, db: Db) -> Response:
  return Response(junit_xml(batch_id, await _batch(db, user, batch_id)), media_type="application/xml; charset=utf-8")


@router.get("/api/testruns/batch/{batch_id}/html")
async def batch_html(batch_id: str, user: CurrentUser, db: Db) -> Response:
  return Response(html_report(batch_id, await _batch(db, user, batch_id)), media_type="text/html; charset=utf-8")


@router.post("/api/testruns/{run_id}/push-to/{integration_type}")
async def push_to_tracker(run_id: str, integration_type: str, user: CurrentUser, db: Db) -> Response:
  """Files a failed run as an Azure DevOps Bug. Idempotent: a repeat push
  returns the issue already created. A tracker-side failure is still a
  recorded attempt (200 with errorMessage), not an HTTP error."""
  if integration_type != azure_devops.INTEGRATION_TYPE:
    raise ApiError(400, f"type must be {azure_devops.INTEGRATION_TYPE}")
  await _run(db, user, run_id)
  try:
    outcome = await defects.push_failed_run(db, run_id)
  except defects.NotConfigured as err:
    raise ApiError(400, str(err)) from err
  return _json(s.issue_push(outcome.push), 201 if outcome.created else 200)


def _json(payload: Any, status: int) -> Response:
  return Response(json.dumps(payload), status_code=status, media_type="application/json")


class RerunIn(BaseModel):
  resumeFromStepOrder: Optional[int] = Field(default=None, gt=0)


@router.post("/api/testruns/{run_id}/rerun", status_code=201)
async def rerun(run_id: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  run = await _run(db, user, run_id)
  project_id = await _run_project(db, user, run)
  try:
    body = RerunIn.model_validate(await json_body(request) or {})
  except ValidationError:
    body = RerunIn()
  await db.commit()
  try:
    return await executor.rerun(run_id, project_emit(project_id), body.resumeFromStepOrder)
  except Exception as err:  # noqa: BLE001
    raise ApiError(500, str(err)) from err


# --- Review queues: self-healing and visual diffs -----------------------------


async def _step_project(db: Db, test_step_id: str) -> str:
  row = (
    await db.execute(
      select(m.TestCase.project_id).join(m.TestStep, m.TestStep.test_case_id == m.TestCase.id).where(m.TestStep.id == test_step_id)
    )
  ).scalar_one()
  return row


@router.get("/api/healing-events")
async def list_healing_events(user: CurrentUser, db: Db, projectId: Optional[str] = Query(default=None)) -> list[dict[str, Any]]:  # noqa: N803
  if not projectId:
    raise ApiError(400, "projectId query param is required")
  await require_project_role(db, user, projectId, "viewer")
  rows = await db.execute(
    select(m.HealingEvent)
    .join(m.TestStep, m.TestStep.id == m.HealingEvent.test_step_id)
    .join(m.TestCase, m.TestCase.id == m.TestStep.test_case_id)
    .where(m.TestCase.project_id == projectId, m.HealingEvent.approved.is_(False), m.HealingEvent.dismissed.is_(False))
    .order_by(m.HealingEvent.created_at.desc())
  )
  return [s.healing_event(e) for e in rows.scalars()]


class ApproveHealingIn(BaseModel):
  makePrimary: bool = False


async def _healing_event(db: Db, user: CurrentUser, event_id: str) -> m.HealingEvent:
  event = await db.get(m.HealingEvent, event_id)
  if event is None:
    raise ApiError(404, "Healing event not found")
  await require_project_role(db, user, await _step_project(db, event.test_step_id), "editor")
  return event


@router.post("/api/healing-events/{event_id}/approve")
async def approve_healing(event_id: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  """Adds the healed locator as an alternate (optionally the primary) --
  nothing changes on a step without this explicit approval."""
  event = await _healing_event(db, user, event_id)
  body = parse_body(ApproveHealingIn, await json_body(request))
  step = await db.get(m.TestStep, event.test_step_id)
  assert step is not None
  candidates = json.loads(step.locator_candidates) if step.locator_candidates else []
  step.locator_candidates = json.dumps([*candidates, json.loads(event.new_selector)])
  if body.makePrimary:
    step.selector = event.new_selector
  event.approved = True
  remember(db, "healing", event.id)
  await db.flush()
  return s.healing_event(event)


@router.post("/api/healing-events/{event_id}/dismiss")
async def dismiss_healing(event_id: str, user: CurrentUser, db: Db) -> dict[str, Any]:
  event = await _healing_event(db, user, event_id)
  event.dismissed = True
  await db.flush()
  return s.healing_event(event)


@router.get("/api/visual-diffs")
async def list_visual_diffs(user: CurrentUser, db: Db, projectId: Optional[str] = Query(default=None)) -> list[dict[str, Any]]:  # noqa: N803
  if not projectId:
    raise ApiError(400, "projectId query param is required")
  await require_project_role(db, user, projectId, "viewer")
  rows = await db.execute(
    select(m.VisualDiff)
    .join(m.TestStep, m.TestStep.id == m.VisualDiff.test_step_id)
    .join(m.TestCase, m.TestCase.id == m.TestStep.test_case_id)
    .where(m.TestCase.project_id == projectId, m.VisualDiff.status == "pending")
    .order_by(m.VisualDiff.created_at.desc())
  )
  return [s.visual_diff(d) for d in rows.scalars()]


async def _visual_diff(db: Db, user: CurrentUser, diff_id: str) -> m.VisualDiff:
  diff = await db.get(m.VisualDiff, diff_id)
  if diff is None:
    raise ApiError(404, "Visual diff not found")
  await require_project_role(db, user, await _step_project(db, diff.test_step_id), "editor")
  return diff


@router.post("/api/visual-diffs/{diff_id}/approve")
async def approve_visual_diff(diff_id: str, user: CurrentUser, db: Db) -> dict[str, Any]:
  """Promotes the new screenshot to the baseline -- the only way a baseline changes."""
  diff = await _visual_diff(db, user, diff_id)
  await approve_diff(db, diff)
  return s.visual_diff(diff)


@router.post("/api/visual-diffs/{diff_id}/reject")
async def reject_visual_diff(diff_id: str, user: CurrentUser, db: Db) -> dict[str, Any]:
  diff = await _visual_diff(db, user, diff_id)
  diff.status, diff.resolved_at = "rejected", utcnow()
  await db.flush()
  return s.visual_diff(diff)
