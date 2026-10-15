"""/api/testcases/{id}/... and /api/change-requests."""

from __future__ import annotations

import json
from typing import Any, Literal, Optional

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, select

from .. import chat
from ..agents.schemas import TestCaseUpdatesIn
from ..auth.sessions import CurrentUser, can_review_test_cases, require_project_role
from ..db import models as m
from ..db import records, test_cases
from ..db import serializers as s
from ..db.base import new_id, utcnow
from ..db.session import Db
from ..memory.learning import forget, remember
from ..errors import ApiError, parse_body
from ..execution import executor
from ..execution.exporter import export_test_case, file_name_for
from ..execution.test_data import TestDataParseError, parse_test_data
from ..ws.registry import broadcast, project_emit
from .common import can_edit, json_body, no_content

router = APIRouter()


class TestCaseEditIn(TestCaseUpdatesIn):
  status: Optional[Literal["draft", "approved", "archived"]] = None


class ReviewIn(BaseModel):
  decision: Literal["approve", "reject"]
  note: Optional[str] = None


class GenerateStepIn(BaseModel):
  instruction: str = Field(min_length=1)


class RunIn(BaseModel):
  environmentId: Optional[str] = None
  continueFromChatSession: Optional[bool] = None


class InsertFlowIn(BaseModel):
  flowVersionId: str = Field(min_length=1)


class UpdateFlowBlockIn(BaseModel):
  sourceFlowVersionId: str = Field(min_length=1)
  toFlowVersionId: str = Field(min_length=1)


class DataSetIn(BaseModel):
  text: Optional[str] = Field(default=None, min_length=1)
  format: Optional[Literal["csv", "json"]] = None
  secretColumns: Optional[list[str]] = None


class ChangeRequestIn(BaseModel):
  note: str = Field(min_length=1)


async def _test_case(db: Db, user: CurrentUser, test_case_id: str, min_role: str) -> tuple[m.TestCase, str]:
  tc = await db.get(m.TestCase, test_case_id)
  if tc is None:
    raise ApiError(404, "Test case not found")
  role = await require_project_role(db, user, tc.project_id, min_role)
  return tc, role


def _require_owner(tc: m.TestCase, user: CurrentUser, message: str) -> None:
  if not can_edit(tc.created_by_id, user):
    raise ApiError(403, message)


def _optional_body(model: type[RunIn], raw: Any) -> RunIn:
  try:
    return model.model_validate(raw or {})
  except ValidationError:
    return model()


@router.get("/api/testcases/{test_case_id}")
async def get_test_case(test_case_id: str, user: CurrentUser, db: Db) -> dict[str, Any]:
  tc, _ = await _test_case(db, user, test_case_id, "viewer")
  return (await test_cases.records(db, [tc], with_provenance=True))[0]


@router.put("/api/testcases/{test_case_id}")
async def update_test_case(test_case_id: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  body = parse_body(TestCaseEditIn, await json_body(request))
  tc, _ = await _test_case(db, user, test_case_id, "editor")
  _require_owner(tc, user, "Only the owner or an admin can edit this test case.")
  # Approving is the reviewer-gated POST /review, never a direct edit (sending
  # back "approved" unchanged is fine -- the editor form round-trips it).
  if body.status == "approved" and tc.status != "approved":
    raise ApiError(
      400,
      'Approving a test case requires "reviewer" or "owner" access and must go through the review action, not a direct edit.',
    )
  if body.status:
    tc.status = body.status
  updated = await test_cases.apply_update(db, tc.project_id, test_case_id, body.to_dict(), user.id)
  if updated is None:
    raise ApiError(404, "Test case not found")
  broadcast(tc.project_id, {"type": "testcases.updated", "testCase": updated})
  return updated


@router.delete("/api/testcases/{test_case_id}")
async def delete_test_case(test_case_id: str, user: CurrentUser, db: Db) -> Response:
  if await db.get(m.TestCase, test_case_id) is None:
    return no_content()
  tc, _ = await _test_case(db, user, test_case_id, "editor")
  _require_owner(tc, user, "Only the owner or an admin can delete this test case.")
  project_id, title = tc.project_id, tc.title
  await db.delete(tc)
  forget(db, project_id, "test_case", test_case_id)
  await records.record_audit(
    db, user_id=user.id, project_id=project_id, action="testcase.deleted", target_type="TestCase", target_id=test_case_id, detail=title
  )
  return no_content()


@router.post("/api/testcases/{test_case_id}/review")
async def review_test_case(test_case_id: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  """Approve/reject: reviewer or owner only, and never your own test case
  (authoring and approving are separate duties)."""
  tc, role = await _test_case(db, user, test_case_id, "viewer")
  if not can_review_test_cases(role):
    raise ApiError(403, 'This action requires "reviewer" or "owner" access to this project.')
  if tc.created_by_id == user.id:
    raise ApiError(403, "You can't approve or reject your own test case -- ask another reviewer or the project owner.")
  body = parse_body(ReviewIn, await json_body(request))
  tc.status = "approved" if body.decision == "approve" else "draft"
  tc.updated_at = utcnow()
  remember(db, "test_case", tc.id)
  await records.record_audit(
    db,
    user_id=user.id,
    project_id=tc.project_id,
    action="testcase.approved" if body.decision == "approve" else "testcase.rejected",
    target_type="TestCase",
    target_id=tc.id,
    detail=body.note if body.note is not None else tc.title,
  )
  await db.flush()
  record = (await test_cases.records(db, [tc], with_provenance=True))[0]
  broadcast(tc.project_id, {"type": "testcases.updated", "testCase": record})
  return record


async def _data_set(db: Db, test_case_id: str) -> Optional[m.TestDataSet]:
  return (await db.execute(select(m.TestDataSet).where(m.TestDataSet.test_case_id == test_case_id))).scalar_one_or_none()


@router.get("/api/testcases/{test_case_id}/export")
async def export(test_case_id: str, user: CurrentUser, db: Db) -> Response:
  tc, _ = await _test_case(db, user, test_case_id, "viewer")
  record = (await test_cases.records(db, [tc]))[0]
  data_set = await _data_set(db, test_case_id)
  script = export_test_case(record, s.test_data_set(data_set) if data_set else None)
  return Response(
    script,
    media_type="text/plain; charset=utf-8",
    headers={"Content-Disposition": f'attachment; filename="{file_name_for(tc.title)}"'},
  )


@router.post("/api/testcases/{test_case_id}/steps/generate")
async def generate_step(test_case_id: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  body = parse_body(GenerateStepIn, await json_body(request), "instruction is required")
  tc, _ = await _test_case(db, user, test_case_id, "editor")
  url = await db.get(m.TargetUrl, tc.url_id) if tc.url_id else None
  if url is None:
    raise ApiError(400, "This test case has no target URL to inspect.")
  project_id, target = tc.project_id, url.url
  await db.commit()
  try:
    return await chat.generate_step(project_id, target, body.instruction)
  except Exception as err:  # noqa: BLE001
    raise ApiError(500, str(err)) from err


@router.post("/api/testcases/{test_case_id}/run", status_code=201)
async def run_test_case(test_case_id: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  tc, _ = await _test_case(db, user, test_case_id, "viewer")
  body = _optional_body(RunIn, await json_body(request))
  project_id = tc.project_id
  await db.commit()  # don't hold a transaction open for the whole run
  try:
    return await executor.start_test_case(
      test_case_id,
      "user",
      project_emit(project_id),
      executor.ExecuteOptions(
        environment_id=body.environmentId,
        continue_from_chat_session=bool(body.continueFromChatSession),
        user_id=user.id,
      ),
    )
  except Exception as err:  # noqa: BLE001
    raise ApiError(500, str(err)) from err


@router.post("/api/testcases/{test_case_id}/steps/insert-flow")
async def insert_flow(test_case_id: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  tc, _ = await _test_case(db, user, test_case_id, "editor")
  _require_owner(tc, user, "Only the owner or an admin can edit this test case.")
  body = parse_body(InsertFlowIn, await json_body(request))
  try:
    updated = await test_cases.insert_flow(db, test_case_id, body.flowVersionId)
  except ValueError as err:
    raise ApiError(400, str(err)) from err
  assert updated is not None
  broadcast(tc.project_id, {"type": "testcases.updated", "testCase": updated})
  return updated


@router.post("/api/testcases/{test_case_id}/steps/update-flow-block")
async def update_flow_block(test_case_id: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  tc, _ = await _test_case(db, user, test_case_id, "editor")
  _require_owner(tc, user, "Only the owner or an admin can edit this test case.")
  body = parse_body(UpdateFlowBlockIn, await json_body(request))
  try:
    updated = await test_cases.update_flow_block(db, test_case_id, body.sourceFlowVersionId, body.toFlowVersionId)
  except ValueError as err:
    raise ApiError(400, str(err)) from err
  assert updated is not None
  broadcast(tc.project_id, {"type": "testcases.updated", "testCase": updated})
  return updated


# --- Data sets ----------------------------------------------------------------


@router.get("/api/testcases/{test_case_id}/data-set")
async def get_data_set(test_case_id: str, user: CurrentUser, db: Db) -> Optional[dict[str, Any]]:
  await _test_case(db, user, test_case_id, "viewer")
  row = await _data_set(db, test_case_id)
  return s.test_data_set(row) if row else None


@router.put("/api/testcases/{test_case_id}/data-set")
async def put_data_set(test_case_id: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  """With text+format: replace the data set. Without: only re-flag which
  existing columns are secret."""
  tc, _ = await _test_case(db, user, test_case_id, "editor")
  _require_owner(tc, user, "Only the owner or an admin can edit this test case's data set.")
  body = parse_body(DataSetIn, await json_body(request))
  row = await _data_set(db, test_case_id)

  if not body.text or not body.format:
    if row is None:
      raise ApiError(404, "No data set to update -- upload one first (with text + format).")
    columns = json.loads(row.columns)
    row.secret_columns = json.dumps([c for c in body.secretColumns or [] if c in columns])
    row.updated_at = utcnow()
    await db.flush()
    return s.test_data_set(row)

  try:
    parsed = parse_test_data(body.text, body.format)
  except TestDataParseError as err:
    raise ApiError(400, str(err)) from err
  # A stale secret flag can't survive a re-upload that dropped that column.
  secret = [c for c in body.secretColumns or [] if c in parsed.columns]
  if row is None:
    row = m.TestDataSet(id=new_id(), test_case_id=test_case_id, columns="", secret_columns="", rows="", source="")
    db.add(row)
  row.columns, row.secret_columns = json.dumps(parsed.columns), json.dumps(secret)
  row.rows, row.source, row.updated_at = json.dumps(parsed.rows), body.format, utcnow()
  await db.flush()
  return s.test_data_set(row)


@router.delete("/api/testcases/{test_case_id}/data-set")
async def delete_data_set(test_case_id: str, user: CurrentUser, db: Db) -> Response:
  tc, _ = await _test_case(db, user, test_case_id, "editor")
  _require_owner(tc, user, "Only the owner or an admin can edit this test case's data set.")
  await db.execute(delete(m.TestDataSet).where(m.TestDataSet.test_case_id == test_case_id))
  return no_content()


@router.post("/api/testcases/{test_case_id}/run-with-data", status_code=202)
async def run_with_data(test_case_id: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  tc, _ = await _test_case(db, user, test_case_id, "viewer")
  body = _optional_body(RunIn, await json_body(request))
  project_id = tc.project_id
  await db.commit()
  try:
    return await executor.run_across_data_set(test_case_id, body.environmentId, project_emit(project_id))
  except Exception as err:  # noqa: BLE001
    raise ApiError(500, str(err)) from err


# --- Change requests ------------------------------------------------------------


async def _change_request_record(db: Db, row: m.ChangeRequest) -> dict[str, Any]:
  tc = await db.get(m.TestCase, row.test_case_id)
  requester = await db.get(m.User, row.requested_by_id)
  assert tc is not None and requester is not None
  return s.change_request(row, tc.title, requester)


@router.get("/api/testcases/{test_case_id}/change-requests")
async def list_change_requests(test_case_id: str, user: CurrentUser, db: Db) -> list[dict[str, Any]]:
  await _test_case(db, user, test_case_id, "viewer")
  rows = (
    await db.execute(
      select(m.ChangeRequest).where(m.ChangeRequest.test_case_id == test_case_id).order_by(m.ChangeRequest.created_at.desc())
    )
  ).scalars()
  return [await _change_request_record(db, row) for row in rows]


@router.post("/api/testcases/{test_case_id}/change-requests", status_code=201)
async def create_change_request(test_case_id: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  """Anyone with access can flag a test case for its owner to fix."""
  body = parse_body(ChangeRequestIn, await json_body(request), "note is required")
  await _test_case(db, user, test_case_id, "viewer")
  row = m.ChangeRequest(id=new_id(), test_case_id=test_case_id, requested_by_id=user.id, note=body.note)
  db.add(row)
  await db.flush()
  return await _change_request_record(db, row)


@router.post("/api/change-requests/{change_request_id}/resolve")
async def resolve_change_request(change_request_id: str, user: CurrentUser, db: Db) -> dict[str, Any]:
  row = await db.get(m.ChangeRequest, change_request_id)
  if row is None:
    raise ApiError(404, "Change request not found")
  tc = await db.get(m.TestCase, row.test_case_id)
  assert tc is not None
  await require_project_role(db, user, tc.project_id, "viewer")
  if tc.created_by_id != user.id and user.role != "admin":
    raise ApiError(403, "Only the test case owner or an admin can resolve this.")
  row.status, row.resolved_at = "resolved", utcnow()
  await db.flush()
  return await _change_request_record(db, row)
