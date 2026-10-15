"""Per-request database session (FastAPI dependency): commits when the
handler succeeds, rolls back when it raises."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends

from sqlalchemy.ext.asyncio import AsyncSession

from .base import sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
  async with sessionmaker()() as session:
    try:
      yield session
      await session.commit()
    except BaseException:
      await session.rollback()
      raise


# Use this in route signatures. scope="function" commits *before* the
# response is sent, so a client never reads its own write as missing.
Db = Annotated[AsyncSession, Depends(get_session, scope="function")]
