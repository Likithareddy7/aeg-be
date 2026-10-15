"""/api/auth, /api/users, /api/admin -- sign-in, the user's own password, and
admin-only user management."""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, select

from .. import services
from ..auth.passwords import hash_password, verify_password
from ..auth.sessions import SESSION_COOKIE_NAME, SESSION_TTL, CurrentUser, create_session, destroy_session
from ..config import settings
from ..db import models as m
from ..db import serializers as s
from ..db.session import Db
from ..errors import ApiError, parse_body
from .common import json_body, no_content

router = APIRouter()


class LoginIn(BaseModel):
  username: str = Field(min_length=1)
  password: str = Field(min_length=1)


class ChangePasswordIn(BaseModel):
  currentPassword: str = Field(min_length=1)
  newPassword: str = Field(min_length=6)


class CreateUserIn(BaseModel):
  username: str = Field(min_length=1)
  password: str = Field(min_length=6)
  role: Literal["admin", "member"] = "member"


class UpdateUserIn(BaseModel):
  password: Optional[str] = Field(default=None, min_length=6)
  role: Optional[Literal["admin", "member"]] = None

  @model_validator(mode="after")
  def _require_a_change(self) -> "UpdateUserIn":
    if self.password is None and self.role is None:
      raise ValueError("Provide a password and/or role to update.")
    return self


@router.post("/api/auth/login")
async def login(request: Request, db: Db) -> Response:
  body = parse_body(LoginIn, await json_body(request), "username and password are required")
  user = (await db.execute(select(m.User).where(m.User.username == body.username))).scalar_one_or_none()
  if user is None or not verify_password(body.password, user.password_hash):
    raise ApiError(401, "Invalid username or password")
  session_id, session = await create_session(db, user.id)
  response = JSONResponse(s.user_summary(user))
  response.set_cookie(
    SESSION_COOKIE_NAME,
    session_id,
    httponly=True,
    samesite="lax",
    expires=session.expires_at.strftime("%a, %d %b %Y %H:%M:%S GMT"),
    max_age=int(SESSION_TTL.total_seconds()),
    path="/",
  )
  return response


@router.post("/api/auth/logout")
async def logout(request: Request, db: Db) -> Response:
  session_id = request.cookies.get(SESSION_COOKIE_NAME)
  if session_id:
    await destroy_session(db, session_id)
  response = no_content()
  response.delete_cookie(SESSION_COOKIE_NAME, path="/")
  return response


@router.get("/api/auth/me")
async def me(user: CurrentUser) -> dict[str, str]:
  return user.as_dict()


@router.put("/api/auth/password")
async def change_password(request: Request, user: CurrentUser, db: Db) -> Response:
  """Self-service: proves the current password, and signs out every *other*
  session -- not the one making the change."""
  body = parse_body(ChangePasswordIn, await json_body(request))
  row = await db.get(m.User, user.id)
  if row is None or not verify_password(body.currentPassword, row.password_hash):
    raise ApiError(401, "Current password is incorrect.")
  row.password_hash = hash_password(body.newPassword)
  current = request.cookies.get(SESSION_COOKIE_NAME)
  query = delete(m.Session).where(m.Session.user_id == user.id)
  if current:
    query = query.where(m.Session.id != current)
  await db.execute(query)
  return no_content()


def _require_admin(user: Any, message: str) -> None:
  if user.role != "admin":
    raise ApiError(403, message)


@router.get("/api/users")
async def list_users(user: CurrentUser, db: Db) -> list[dict[str, Any]]:
  _require_admin(user, "Only an admin can view the user list.")
  rows = await db.execute(select(m.User).order_by(m.User.created_at))
  return [s.user_summary(u) for u in rows.scalars()]  # type: ignore[misc]


@router.post("/api/users", status_code=201)
async def create_user(request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  _require_admin(user, "Only an admin can add teammates.")
  body = parse_body(CreateUserIn, await json_body(request))
  if (await db.execute(select(m.User.id).where(m.User.username == body.username))).first():
    raise ApiError(409, "That username is already taken.")
  row = m.User(username=body.username, password_hash=hash_password(body.password), role=body.role)
  db.add(row)
  await db.flush()
  return s.user_summary(row)  # type: ignore[return-value]


@router.put("/api/users/{user_id}")
async def update_user(user_id: str, request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
  """Admin edit: role and/or password. A password reset signs that user out
  everywhere; a role change doesn't."""
  _require_admin(user, "Only an admin can edit teammates.")
  body = parse_body(UpdateUserIn, await json_body(request))
  if user_id == user.id and body.role == "member":
    raise ApiError(400, "You can't remove your own admin access.")
  row = await db.get(m.User, user_id)
  if row is None:
    raise ApiError(404, "User not found")
  if body.role:
    row.role = body.role
  if body.password:
    row.password_hash = hash_password(body.password)
    await db.execute(delete(m.Session).where(m.Session.user_id == user_id))
  await db.flush()
  return s.user_summary(row)  # type: ignore[return-value]


@router.get("/api/admin/config")
async def admin_config(user: CurrentUser) -> dict[str, Any]:
  _require_admin(user, "Admin only.")
  return {
    "model": services.get().model_name,
    "credentialEncryptionConfigured": bool(settings.credential_encryption_key),
    "defaultBrowser": settings.playwright_browser,
    "defaultHeadless": settings.playwright_headless,
    "maxParallelRuns": settings.max_parallel_runs,
    "webhookConfigured": bool(settings.webhook_url),
  }
