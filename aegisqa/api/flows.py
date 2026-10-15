"""/api/flows and /api/environments -- reached by their own id, so each route
resolves the parent project before checking access."""

from __future__ import annotations

import json
from typing import Any, Literal, Optional

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import func, select

from ..agents.schemas import StepIn
from ..auth.sessions import CurrentUser, require_project_role
from ..crypto import encrypt_secret
from ..db import models as m
from ..db import records
from ..db import serializers as s
from ..db.base import iso, new_id, utcnow
from ..db.session import Db
from ..errors import ApiError, parse_body
from .common import can_edit, json_body, no_content

router = APIRouter()


async def flow_record(db: Db, flow: m.Flow) -> dict[str, Any]:
  versions = list(
    (await db.execute(select(m.FlowVersion).where(m.FlowVersion.flow_id == flow.id).order_by(m.FlowVersion.version))).scalars()
  )
  user_ids = {flow.created_by_id, *(v.created_by_id for v in versions)} - {None}
  users = {u.id: u for u in (await db.execute(select(m.User).where(m.User.id.in_(user_ids)))).scalars()} if user_ids else {}
  return {
    "id": flow.id,
    "projectId": flow.project_id,
    "name": flow.name,
    "description": flow.description,
    "createdBy": s.user_summary(users.get(flow.created_by_id or "")),
    "versions": [s.flow_version(v, users.get(v.created_by_id or "")) for v in versions],
    "createdAt": iso(flow.created_at),
    "updatedAt": iso(flow.updated_at),
  }


class FlowMetadataIn(BaseModel):
  name: Optional[str] = Field(default=None, min_length=1)
  description: Optional[str] = None


class FlowVersionIn(BaseModel):
  steps: list[StepIn]
  note: Optional[str] = None


async def _editable_flow(db: Db, user: CurrentUser, flow_id: str, verb: str) -> m.Flow:
  flow = await db.get(m.Flow, flow_id)
  if flow is None:
    raise ApiError(404, "Flow not found")
  await require_project_role(db, user, flow.project_id, "editor")
  if not can_edit(flow.created_by_id, user):
    raise ApiError(403, f"Only the creator or an admin can {verb} this flow.")
  return flow


@router.get("/api/flows/{flow_id}")
async def get_flow(flow_id: str, user: CurrentUser, db: Db) -> dict[str, Any]:
  flow = await db.get(m.Flow, flow_id)
  if flow is None:
    raise ApiError(404, "Flow not found")
  await require_project_role(db, user, flow.project_id, "viewer")
  return await flow_record(db, flow)


@router.put("/api/flows/{flow_id}")
async def update_flow(flow_id: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  flow = await _editable_flow(db, user, flow_id, "edit")
  raw = await json_body(request) or {}
  body = parse_body(FlowMetadataIn, raw)
  if body.name is not None:
    flow.name = body.name
  if "description" in raw:
    flow.description = body.description
  flow.updated_at = utcnow()
  await db.flush()
  return await flow_record(db, flow)


@router.delete("/api/flows/{flow_id}")
async def delete_flow(flow_id: str, user: CurrentUser, db: Db) -> Response:
  if await db.get(m.Flow, flow_id) is None:
    return no_content()
  flow = await _editable_flow(db, user, flow_id, "delete")
  await db.delete(flow)
  return no_content()


@router.post("/api/flows/{flow_id}/versions", status_code=201)
async def create_flow_version(flow_id: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  """A new immutable version -- test cases that inserted an older one keep
  running exactly what they inserted until someone applies the update."""
  flow = await _editable_flow(db, user, flow_id, "edit")
  body = parse_body(FlowVersionIn, await json_body(request))
  latest = (await db.execute(select(func.coalesce(func.max(m.FlowVersion.version), 0)).where(m.FlowVersion.flow_id == flow_id))).scalar_one()
  version = m.FlowVersion(
    id=new_id(),
    flow_id=flow_id,
    version=latest + 1,
    steps=json.dumps([st.to_dict() for st in body.steps]),
    note=body.note,
    created_by_id=user.id,
  )
  db.add(version)
  flow.updated_at = utcnow()
  await db.flush()
  creator = await db.get(m.User, user.id)
  return s.flow_version(version, creator)


# --- Environments ------------------------------------------------------------


class EnvironmentUpdateIn(BaseModel):
  name: Optional[str] = Field(default=None, min_length=1)
  baseUrl: Optional[HttpUrl] = None
  browser: Optional[Literal["chromium", "firefox", "webkit"]] = None
  headless: Optional[bool] = None
  viewportWidth: Optional[int] = Field(default=None, gt=0)
  viewportHeight: Optional[int] = Field(default=None, gt=0)
  isDefault: Optional[bool] = None


class CredentialsIn(BaseModel):
  values: dict[str, str]


async def _environment(db: Db, user: CurrentUser, environment_id: str, min_role: str) -> m.Environment:
  env = await db.get(m.Environment, environment_id)
  if env is None:
    raise ApiError(404, "Environment not found")
  await require_project_role(db, user, env.project_id, min_role)
  return env


@router.put("/api/environments/{environment_id}")
async def update_environment(environment_id: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  """Settings are editor-level; credentials and deletion need owner."""
  env = await _environment(db, user, environment_id, "editor")
  raw = await json_body(request) or {}
  body = parse_body(EnvironmentUpdateIn, raw)
  fields = {
    "name": "name",
    "browser": "browser",
    "headless": "headless",
    "viewportWidth": "viewport_width",
    "viewportHeight": "viewport_height",
    "isDefault": "is_default",
  }
  for key, attr in fields.items():
    if getattr(body, key) is not None:
      setattr(env, attr, getattr(body, key))
  if body.baseUrl is not None:
    env.base_url = str(raw["baseUrl"])
  env.updated_at = utcnow()
  await db.flush()
  return s.environment(env)


@router.delete("/api/environments/{environment_id}")
async def delete_environment(environment_id: str, user: CurrentUser, db: Db) -> Response:
  env = await _environment(db, user, environment_id, "owner")
  project_id = env.project_id
  await db.delete(env)
  await records.record_audit(
    db, user_id=user.id, project_id=project_id, action="environment.deleted", target_type="Environment", target_id=environment_id
  )
  return no_content()


@router.put("/api/environments/{environment_id}/credentials")
async def set_credentials(environment_id: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  """Encrypted at rest; never returned by any API (only `hasCredentials`)."""
  env = await _environment(db, user, environment_id, "owner")
  body = parse_body(CredentialsIn, await json_body(request), "Body must be { values: Record<string, string> }")
  try:
    env.credentials_encrypted = encrypt_secret(json.dumps(body.values))
  except ValueError as err:
    raise ApiError(400, str(err)) from err
  env.updated_at = utcnow()
  await records.record_audit(
    db,
    user_id=user.id,
    project_id=env.project_id,
    action="environment.credentials_updated",
    target_type="Environment",
    target_id=environment_id,
  )
  return s.environment(env)
