"""Engine/session setup and the small helpers every repo uses."""

from __future__ import annotations

import os
import secrets
import string
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from ..config import settings


class Base(DeclarativeBase):
  pass


_engine: Optional[AsyncEngine] = None
_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None


def engine() -> AsyncEngine:
  global _engine, _sessionmaker
  if _engine is None:
    if not settings.database_url:
      raise RuntimeError("DATABASE_URL is not set -- point it at your PostgreSQL database.")
    _engine = create_async_engine(settings.async_database_url, pool_pre_ping=True)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
  return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
  engine()
  assert _sessionmaker is not None
  return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
  """A session for work outside a request (WebSocket handlers, test runs,
  agent tools). Commits on success, rolls back on error."""
  async with sessionmaker()() as session:
    try:
      yield session
      await session.commit()
    except BaseException:
      await session.rollback()
      raise


async def dispose_engine() -> None:
  global _engine, _sessionmaker
  if _engine is not None:
    await _engine.dispose()
  _engine = None
  _sessionmaker = None


# ---------------------------------------------------------------------------
# IDs and timestamps
# ---------------------------------------------------------------------------

_ALPHABET = string.digits + string.ascii_lowercase
_counter = int.from_bytes(os.urandom(2), "big")
_counter_lock = threading.Lock()


def _base36(value: int, width: int) -> str:
  out = ""
  while value:
    value, rem = divmod(value, 36)
    out = _ALPHABET[rem] + out
  return out.rjust(width, "0")[-width:]


def new_id() -> str:
  """A 25-character, roughly time-ordered, collision-resistant id in the same
  shape as the cuid()s already stored in the database."""
  global _counter
  with _counter_lock:
    _counter = (_counter + 1) % (36**4)
    counter = _counter
  timestamp = _base36(int(time.time() * 1000), 8)
  random_part = "".join(secrets.choice(_ALPHABET) for _ in range(12))
  return "c" + timestamp + _base36(counter, 4) + random_part


def utcnow() -> datetime:
  """Naive UTC, millisecond precision -- matches the TIMESTAMP(3) columns."""
  now = datetime.now(timezone.utc).replace(tzinfo=None)
  return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def iso(value: Optional[datetime]) -> Optional[str]:
  """JavaScript's Date#toISOString() format, which the frontend expects:
  2026-10-12T19:17:23.847Z."""
  if value is None:
    return None
  if value.tzinfo is not None:
    value = value.astimezone(timezone.utc).replace(tzinfo=None)
  return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def ms_between(start: datetime, end: datetime) -> int:
  return int((end - start).total_seconds() * 1000)
