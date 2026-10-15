"""Server-side sessions behind an httpOnly `sid` cookie, plus the per-project
role checks every route uses.

This is the single place a request's identity is established, for both REST
and WebSocket. To connect the client's SSO later, replace
`user_for_session` / `current_user` with token validation -- nothing else
needs to change.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Optional

from fastapi import Depends, Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import models as m
from ..db.base import utcnow
from ..db.session import Db
from ..errors import ApiError

SESSION_COOKIE_NAME = "sid"
SESSION_TTL = timedelta(days=7)

ROLE_RANK = {"viewer": 0, "reviewer": 1, "editor": 2, "owner": 3}


@dataclass(frozen=True)
class SessionUser:
  id: str
  username: str
  role: str  # "admin" | "member"

  def as_dict(self) -> dict[str, str]:
    return {"id": self.id, "username": self.username, "role": self.role}


async def create_session(db: AsyncSession, user_id: str) -> tuple[str, m.Session]:
  session_id = secrets.token_hex(32)
  row = m.Session(id=session_id, user_id=user_id, expires_at=utcnow() + SESSION_TTL)
  db.add(row)
  await db.flush()
  return session_id, row


async def destroy_session(db: AsyncSession, session_id: str) -> None:
  await db.execute(delete(m.Session).where(m.Session.id == session_id))


async def user_for_session(db: AsyncSession, session_id: Optional[str]) -> Optional[SessionUser]:
  if not session_id:
    return None
  row = (
    await db.execute(select(m.Session, m.User).join(m.User, m.User.id == m.Session.user_id).where(m.Session.id == session_id))
  ).first()
  if row is None:
    return None
  session, user = row
  if session.expires_at < utcnow():
    await db.execute(delete(m.Session).where(m.Session.id == session.id))
    await db.commit()
    return None
  return SessionUser(id=user.id, username=user.username, role="admin" if user.role == "admin" else "member")


async def current_user(request: Request, db: Db) -> SessionUser:
  user = await user_for_session(db, request.cookies.get(SESSION_COOKIE_NAME))
  if user is None:
    raise ApiError(401, "Not authenticated")
  return user


CurrentUser = Annotated[SessionUser, Depends(current_user)]


def role_meets(role: str, min_role: str) -> bool:
  return ROLE_RANK.get(role, -1) >= ROLE_RANK[min_role]


def can_review_test_cases(role: str) -> bool:
  """Approve/reject is reviewer or owner only -- editor is excluded even though
  it outranks reviewer: authoring and approving are separate capabilities."""
  return role in ("owner", "reviewer")


async def effective_project_role(db: AsyncSession, user: SessionUser, project_id: str) -> Optional[str]:
  """A global admin is always "owner"; otherwise the ProjectMember role, or
  None for no access."""
  if user.role == "admin":
    return "owner"
  return (
    await db.execute(
      select(m.ProjectMember.role).where(m.ProjectMember.project_id == project_id, m.ProjectMember.user_id == user.id)
    )
  ).scalar_one_or_none()


async def require_project_role(db: AsyncSession, user: SessionUser, project_id: str, min_role: str) -> str:
  role = await effective_project_role(db, user, project_id)
  if role is None:
    raise ApiError(403, "You are not a member of this project.")
  if not role_meets(role, min_role):
    raise ApiError(403, f'This action requires at least "{min_role}" access to this project.')
  return role
