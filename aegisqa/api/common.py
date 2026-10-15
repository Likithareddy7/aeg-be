"""Helpers shared by the route modules."""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.sessions import SessionUser, require_project_role, role_meets
from ..db import models as m
from ..errors import ApiError


async def json_body(request: Request) -> Any:
  """The parsed JSON body, or None for an empty one."""
  raw = await request.body()
  if not raw.strip():
    return None
  try:
    return json.loads(raw)
  except ValueError as err:
    raise ApiError(400, "Request body is not valid JSON.") from err


def no_content() -> Response:
  return Response(status_code=204)


async def project_role(db: AsyncSession, user: SessionUser, project_id: str, min_role: str = "viewer") -> str:
  """For /api/projects/{id}/... routes: 404 if the project doesn't exist,
  403 if the user isn't a member or lacks `min_role`."""
  if await db.get(m.Project, project_id) is None:
    raise ApiError(404, "Project not found")
  return await require_project_role(db, user, project_id, min_role)


def require_role(role: str, min_role: str) -> None:
  if not role_meets(role, min_role):
    raise ApiError(403, f'This action requires at least "{min_role}" access to this project.')


def can_edit(created_by_id: Optional[str], user: SessionUser) -> bool:
  """Only the creator (or an admin) may edit or delete a test case or flow;
  items with no recorded creator stay editable by anyone."""
  return created_by_id is None or created_by_id == user.id or user.role == "admin"
