"""/api/projects/{id}/... -- projects, membership, URLs, environments, flows,
integrations, dashboards and project-wide runs/exports."""

from __future__ import annotations

import json
from typing import Any, Literal, Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, HttpUrl, ValidationError
from sqlalchemy import and_, delete, select

from .. import services
from ..agents.schemas import StepIn
from ..auth.sessions import CurrentUser
from ..crypto import encrypt_secret
from ..db import analytics, records, test_cases
from ..db import models as m
from ..db import serializers as s
from ..db.base import iso, new_id
from ..db.session import Db
from ..errors import ApiError, parse_body
from ..execution import executor
from ..execution.exporter import build_ci_package
from ..integrations.azure_devops import INTEGRATION_TYPE
from ..memory import KINDS
from ..ws.registry import project_emit
from .common import json_body, no_content, project_role, require_role

router = APIRouter(prefix="/api/projects")

ProjectRole = Literal["owner", "editor", "reviewer", "viewer"]


class NameIn(BaseModel):
  name: str = Field(min_length=1)


class MemberIn(BaseModel):
  username: str = Field(min_length=1)
  role: ProjectRole


class MemberRoleIn(BaseModel):
  role: ProjectRole


class UrlIn(BaseModel):
  url: HttpUrl


class EnvironmentIn(BaseModel):
  name: str = Field(min_length=1)
  baseUrl: HttpUrl
  browser: Literal["chromium", "firefox", "webkit"]
  headless: bool
  viewportWidth: int = Field(gt=0)
  viewportHeight: int = Field(gt=0)
  isDefault: bool


class FlowIn(BaseModel):
  name: str = Field(min_length=1)
  description: Optional[str] = None
  steps: list[StepIn]


class IntegrationIn(BaseModel):
  baseUrl: HttpUrl
  email: str
  projectKey: str = Field(min_length=1)
  apiToken: Optional[str] = Field(default=None, min_length=1)


class RunAllIn(BaseModel):
  environmentId: Optional[str] = None
  continueFromChatSession: Optional[bool] = None


async def _project_record(db: Db, project: m.Project, role: Optional[str]) -> dict[str, Any]:
  urls = (await db.execute(select(m.TargetUrl).where(m.TargetUrl.project_id == project.id))).scalars()
  return s.project(project, urls, role)


@router.get("")
async def list_projects(user: CurrentUser, db: Db) -> list[dict[str, Any]]:
  if user.role == "admin":
    rows = [(p, "owner") for p in (await db.execute(select(m.Project).order_by(m.Project.created_at.desc()))).scalars()]
  else:
    rows = list(
      (
        await db.execute(
          select(m.Project, m.ProjectMember.role)
          .join(m.ProjectMember, and_(m.ProjectMember.project_id == m.Project.id, m.ProjectMember.user_id == user.id))
          .order_by(m.Project.created_at.desc())
        )
      ).all()
    )
  urls_by_project: dict[str, list[m.TargetUrl]] = {}
  if rows:
    for url in (await db.execute(select(m.TargetUrl).where(m.TargetUrl.project_id.in_([p.id for p, _ in rows])))).scalars():
      urls_by_project.setdefault(url.project_id, []).append(url)
  return [s.project(p, urls_by_project.get(p.id, []), role) for p, role in rows]


@router.post("", status_code=201)
async def create_project(request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  body = parse_body(NameIn, await json_body(request))
  project = m.Project(id=new_id(), name=body.name)
  db.add(project)
  await db.flush()
  db.add(m.ProjectMember(id=new_id(), project_id=project.id, user_id=user.id, role="owner"))
  await records.record_audit(
    db, user_id=user.id, project_id=project.id, action="project.created", target_type="Project", target_id=project.id
  )
  return s.project(project, [], "owner")


@router.get("/{project_id}")
async def get_project(project_id: str, user: CurrentUser, db: Db) -> dict[str, Any]:
  role = await project_role(db, user, project_id)
  project = await db.get(m.Project, project_id)
  assert project is not None
  return await _project_record(db, project, role)


@router.put("/{project_id}")
async def rename_project(project_id: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  role = await project_role(db, user, project_id)
  require_role(role, "owner")
  body = parse_body(NameIn, await json_body(request))
  project = await db.get(m.Project, project_id)
  assert project is not None
  project.name = body.name
  await records.record_audit(
    db, user_id=user.id, project_id=project_id, action="project.renamed", target_type="Project", target_id=project_id, detail=body.name
  )
  return await _project_record(db, project, role)


@router.delete("/{project_id}")
async def delete_project(project_id: str, user: CurrentUser, db: Db) -> Response:
  role = await project_role(db, user, project_id)
  require_role(role, "owner")
  project = await db.get(m.Project, project_id)
  assert project is not None
  # Recorded first: the audit row survives the delete (projectId -> NULL).
  await records.record_audit(
    db, user_id=user.id, project_id=project_id, action="project.deleted", target_type="Project", target_id=project_id, detail=project.name
  )
  await db.delete(project)
  return no_content()


# --- Membership -------------------------------------------------------------


async def _members(db: Db, project_id: str) -> list[dict[str, Any]]:
  rows = await db.execute(
    select(m.ProjectMember, m.User)
    .join(m.User, m.User.id == m.ProjectMember.user_id)
    .where(m.ProjectMember.project_id == project_id)
    .order_by(m.ProjectMember.created_at)
  )
  out = []
  for member, user in rows.all():
    member.user = user
    out.append(s.member(member))
  return out


async def _member(db: Db, project_id: str, user_id: str) -> Optional[dict[str, Any]]:
  row = (
    await db.execute(
      select(m.ProjectMember, m.User)
      .join(m.User, m.User.id == m.ProjectMember.user_id)
      .where(m.ProjectMember.project_id == project_id, m.ProjectMember.user_id == user_id)
    )
  ).first()
  if row is None:
    return None
  member, user = row
  member.user = user
  return s.member(member)


@router.get("/{project_id}/members")
async def list_members(project_id: str, user: CurrentUser, db: Db) -> list[dict[str, Any]]:
  await project_role(db, user, project_id)
  return await _members(db, project_id)


@router.get("/{project_id}/available-members")
async def available_members(project_id: str, user: CurrentUser, db: Db) -> list[dict[str, Any]]:
  """For the owner's "add member" picker: users who aren't members yet."""
  require_role(await project_role(db, user, project_id), "owner")
  members = select(m.ProjectMember.user_id).where(m.ProjectMember.project_id == project_id)
  rows = await db.execute(select(m.User).where(m.User.id.not_in(members)).order_by(m.User.username))
  return [s.user_summary(u) for u in rows.scalars()]  # type: ignore[misc]


@router.post("/{project_id}/members", status_code=201)
async def add_member(project_id: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  require_role(await project_role(db, user, project_id), "owner")
  body = parse_body(MemberIn, await json_body(request))
  target = (await db.execute(select(m.User).where(m.User.username == body.username))).scalar_one_or_none()
  if target is None:
    raise ApiError(404, f'No user named "{body.username}".')
  existing = (
    await db.execute(select(m.ProjectMember).where(m.ProjectMember.project_id == project_id, m.ProjectMember.user_id == target.id))
  ).scalar_one_or_none()
  if existing:
    existing.role = body.role
  else:
    db.add(m.ProjectMember(id=new_id(), project_id=project_id, user_id=target.id, role=body.role))
  await records.record_audit(
    db, user_id=user.id, project_id=project_id, action="member.added", target_type="User", target_id=target.id, detail=f"role={body.role}"
  )
  record = await _member(db, project_id, target.id)
  assert record is not None
  return record


@router.put("/{project_id}/members/{user_id}")
async def update_member(project_id: str, user_id: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  require_role(await project_role(db, user, project_id), "owner")
  body = parse_body(MemberRoleIn, await json_body(request))
  existing = (
    await db.execute(select(m.ProjectMember).where(m.ProjectMember.project_id == project_id, m.ProjectMember.user_id == user_id))
  ).scalar_one_or_none()
  if existing is None:
    raise ApiError(404, "That user isn't a member of this project.")
  existing.role = body.role
  await records.record_audit(
    db, user_id=user.id, project_id=project_id, action="member.role_changed", target_type="User", target_id=user_id, detail=f"role={body.role}"
  )
  record = await _member(db, project_id, user_id)
  assert record is not None
  return record


@router.delete("/{project_id}/members/{user_id}")
async def remove_member(project_id: str, user_id: str, user: CurrentUser, db: Db) -> Response:
  require_role(await project_role(db, user, project_id), "owner")
  await db.execute(delete(m.ProjectMember).where(m.ProjectMember.project_id == project_id, m.ProjectMember.user_id == user_id))
  await records.record_audit(db, user_id=user.id, project_id=project_id, action="member.removed", target_type="User", target_id=user_id)
  return no_content()


@router.get("/{project_id}/audit-log")
async def audit_log(project_id: str, user: CurrentUser, db: Db) -> list[dict[str, Any]]:
  await project_role(db, user, project_id)
  return await records.list_audit(db, project_id)


# --- Target URLs ------------------------------------------------------------


@router.post("/{project_id}/urls", status_code=201)
async def add_url(project_id: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  require_role(await project_role(db, user, project_id), "editor")
  raw = await json_body(request)
  parse_body(UrlIn, raw, "A valid absolute URL is required.")
  url = str(raw["url"])  # stored as entered (HttpUrl would normalize it)
  # Best-effort reachability check -- never blocks creation (localhost or
  # auth-gated apps often don't answer the server), but flags typos early.
  reachable, note = True, None
  try:
    async with httpx.AsyncClient(timeout=5, follow_redirects=True) as client:
      await client.get(url)
  except Exception as err:  # noqa: BLE001
    reachable, note = False, str(err) or err.__class__.__name__
  row = m.TargetUrl(id=new_id(), project_id=project_id, url=url)
  db.add(row)
  await db.flush()
  return {"url": s.target_url(row), "reachable": reachable, "reachabilityNote": note}


# --- Per-project reads --------------------------------------------------------


@router.get("/{project_id}/testcases")
async def list_test_cases(project_id: str, user: CurrentUser, db: Db) -> list[dict[str, Any]]:
  await project_role(db, user, project_id)
  rows = list(
    (await db.execute(select(m.TestCase).where(m.TestCase.project_id == project_id).order_by(m.TestCase.created_at.desc()))).scalars()
  )
  return await test_cases.records(db, rows)


@router.get("/{project_id}/chat")
async def chat_history(project_id: str, user: CurrentUser, db: Db) -> list[dict[str, Any]]:
  """Always the requesting user's own conversation -- chat is private."""
  await project_role(db, user, project_id)
  return await records.list_chat(db, project_id, user.id)


@router.get("/{project_id}/agent-activity")
async def agent_activity(project_id: str, user: CurrentUser, db: Db) -> list[dict[str, Any]]:
  await project_role(db, user, project_id)
  return await records.list_activity(db, project_id, user.id)


@router.get("/{project_id}/usage")
async def usage(project_id: str, user: CurrentUser, db: Db) -> dict[str, Any]:
  await project_role(db, user, project_id)
  return await records.usage_totals(db, project_id)


@router.get("/{project_id}/dashboard")
async def dashboard(project_id: str, user: CurrentUser, db: Db) -> dict[str, Any]:
  await project_role(db, user, project_id)
  return await analytics.dashboard(db, project_id)


@router.get("/{project_id}/failing-tests")
async def failing_tests(project_id: str, user: CurrentUser, db: Db) -> list[dict[str, Any]]:
  await project_role(db, user, project_id)
  return await analytics.failing_tests(db, project_id)


@router.get("/{project_id}/reports")
async def reports(project_id: str, user: CurrentUser, db: Db) -> dict[str, Any]:
  await project_role(db, user, project_id)
  return {"timeSeriesDays": await analytics.runs_time_series(db, project_id)}


@router.get("/{project_id}/memory")
async def search_memory(
  project_id: str,
  user: CurrentUser,
  db: Db,
  q: Optional[str] = None,
  kind: Optional[str] = None,
  module: Optional[str] = None,
  errorCode: Optional[str] = None,  # noqa: N803
  limit: int = 20,
) -> list[dict[str, Any]]:
  """What the platform remembers about this project. `q` searches (keyword +
  semantic); without it, an exact lookup by kind / module / errorCode."""
  await project_role(db, user, project_id)
  memory = services.get().memory
  if memory is None:
    return []
  if kind and kind not in KINDS:
    raise ApiError(400, f"kind must be one of {', '.join(KINDS)}")
  limit = max(1, min(limit, 100))
  if q:
    hits = await memory.search(project_id, q, [kind] if kind else None, limit)
  else:
    hits = await memory.lookup(project_id, kind=kind, module=module, error_code=errorCode, limit=limit)
  return [h.as_dict() for h in hits]


# --- Environments -------------------------------------------------------------


@router.get("/{project_id}/environments")
async def list_environments(project_id: str, user: CurrentUser, db: Db) -> list[dict[str, Any]]:
  await project_role(db, user, project_id)
  rows = await db.execute(select(m.Environment).where(m.Environment.project_id == project_id).order_by(m.Environment.created_at))
  return [s.environment(e) for e in rows.scalars()]


@router.post("/{project_id}/environments", status_code=201)
async def create_environment(project_id: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  require_role(await project_role(db, user, project_id), "editor")
  raw = await json_body(request)
  body = parse_body(EnvironmentIn, raw)
  row = m.Environment(
    id=new_id(),
    project_id=project_id,
    name=body.name,
    base_url=str(raw["baseUrl"]),
    browser=body.browser,
    headless=body.headless,
    viewport_width=body.viewportWidth,
    viewport_height=body.viewportHeight,
    is_default=body.isDefault,
  )
  db.add(row)
  await db.flush()
  return s.environment(row)


# --- Flows ----------------------------------------------------------------------


@router.get("/{project_id}/flows")
async def list_flows(project_id: str, user: CurrentUser, db: Db) -> list[dict[str, Any]]:
  await project_role(db, user, project_id)
  flows = list((await db.execute(select(m.Flow).where(m.Flow.project_id == project_id).order_by(m.Flow.updated_at.desc()))).scalars())
  if not flows:
    return []
  latest: dict[str, m.FlowVersion] = {}
  for version in (await db.execute(select(m.FlowVersion).where(m.FlowVersion.flow_id.in_([f.id for f in flows])))).scalars():
    if version.flow_id not in latest or version.version > latest[version.flow_id].version:
      latest[version.flow_id] = version
  users = {
    u.id: u
    for u in (await db.execute(select(m.User).where(m.User.id.in_({f.created_by_id for f in flows if f.created_by_id})))).scalars()
  }
  return [
    {
      "id": f.id,
      "projectId": f.project_id,
      "name": f.name,
      "description": f.description,
      "createdBy": s.user_summary(users.get(f.created_by_id or "")),
      "latestVersion": latest[f.id].version if f.id in latest else 0,
      "latestStepCount": len(json.loads(latest[f.id].steps)) if f.id in latest else 0,
      "createdAt": iso(f.created_at),
      "updatedAt": iso(f.updated_at),
    }
    for f in flows
  ]


@router.post("/{project_id}/flows", status_code=201)
async def create_flow(project_id: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  from .flows import flow_record

  require_role(await project_role(db, user, project_id), "editor")
  body = parse_body(FlowIn, await json_body(request))
  flow = m.Flow(id=new_id(), project_id=project_id, name=body.name, description=body.description, created_by_id=user.id)
  db.add(flow)
  try:
    await db.flush()
  except Exception as err:  # unique (projectId, name)
    raise ApiError(400, f'A flow named "{body.name}" already exists in this project.') from err
  db.add(
    m.FlowVersion(
      id=new_id(), flow_id=flow.id, version=1, steps=json.dumps([st.to_dict() for st in body.steps]), created_by_id=user.id
    )
  )
  await db.flush()
  return await flow_record(db, flow)


# --- Integrations (Azure DevOps only) -------------------------------------------


@router.get("/{project_id}/integrations")
async def get_integrations(project_id: str, user: CurrentUser, db: Db) -> dict[str, Any]:
  await project_role(db, user, project_id)
  row = (
    await db.execute(select(m.Integration).where(m.Integration.project_id == project_id, m.Integration.type == INTEGRATION_TYPE))
  ).scalar_one_or_none()
  return {INTEGRATION_TYPE: s.integration(row) if row else None}


@router.put("/{project_id}/integrations/{integration_type}")
async def put_integration(project_id: str, integration_type: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  """Project owners manage their own tracker credentials. The token is
  required the first time and optional afterwards (so other fields can be
  edited without rotating it)."""
  require_role(await project_role(db, user, project_id), "owner")
  if integration_type != INTEGRATION_TYPE:
    raise ApiError(400, f"type must be {INTEGRATION_TYPE}")
  raw = await json_body(request)
  body = parse_body(IntegrationIn, raw)
  row = (
    await db.execute(select(m.Integration).where(m.Integration.project_id == project_id, m.Integration.type == integration_type))
  ).scalar_one_or_none()
  try:
    if row is None:
      if not body.apiToken:
        raise ApiError(400, "An API token is required the first time you configure this integration.")
      row = m.Integration(
        id=new_id(),
        project_id=project_id,
        type=integration_type,
        base_url=str(raw["baseUrl"]),
        email=body.email,
        project_key=body.projectKey,
        api_token_encrypted=encrypt_secret(body.apiToken),
      )
      db.add(row)
    else:
      row.base_url, row.email, row.project_key = str(raw["baseUrl"]), body.email, body.projectKey
      if body.apiToken:
        row.api_token_encrypted = encrypt_secret(body.apiToken)
    await db.flush()
  except ValueError as err:  # e.g. CREDENTIAL_ENCRYPTION_KEY not set
    raise ApiError(400, str(err)) from err
  await records.record_audit(
    db,
    user_id=user.id,
    project_id=project_id,
    action="integration.updated",
    target_type="Integration",
    target_id=row.id,
    detail=f"type={integration_type}",
  )
  return s.integration(row)


# --- Project-wide runs and exports ------------------------------------------------


@router.get("/{project_id}/export-ci")
async def export_ci(project_id: str, user: CurrentUser, db: Db) -> Response:
  """Every approved test case as a standalone, runnable Playwright project."""
  await project_role(db, user, project_id)
  rows = list(
    (
      await db.execute(
        select(m.TestCase).where(m.TestCase.project_id == project_id, m.TestCase.status == "approved").order_by(m.TestCase.created_at)
      )
    ).scalars()
  )
  records_ = await test_cases.records(db, rows)
  data_sets = {
    ds.test_case_id: s.test_data_set(ds)
    for ds in (await db.execute(select(m.TestDataSet).where(m.TestDataSet.test_case_id.in_([r.id for r in rows])))).scalars()
  }
  content = build_ci_package(records_, [data_sets.get(r["id"]) for r in records_])
  return Response(
    content,
    media_type="application/zip",
    headers={"Content-Disposition": 'attachment; filename="aegisqa-ci-export.zip"'},
  )


@router.post("/{project_id}/testcases/run-all", status_code=202)
async def run_all(project_id: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  await project_role(db, user, project_id)
  try:
    body = RunAllIn.model_validate(await json_body(request) or {})
  except ValidationError:
    body = RunAllIn()
  await db.commit()  # don't hold a transaction open for the whole batch
  return await executor.run_approved_batch(
    project_id, body.environmentId, project_emit(project_id), bool(body.continueFromChatSession), user.id
  )

