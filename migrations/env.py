"""Alembic environment: async engine on DATABASE_URL, models as metadata."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from aegisqa.config import settings
from aegisqa.db import models  # noqa: F401 -- registers every table on Base.metadata
from aegisqa.db.base import Base
import pgvector.sqlalchemy  # noqa: F401,E402 -- lets autogenerate render the vector type

target_metadata = Base.metadata


def _include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
  # Bookkeeping table left behind in databases first created by the original
  # (Prisma-based) backend -- not part of the schema.
  if type_ == "table" and name == "_prisma_migrations":
    return False
  # Expression / HNSW indexes created by hand in 0004_memory.
  if type_ == "index" and name in ("MemoryRecord_fulltext_idx", "MemoryRecord_embedding_idx"):
    return False
  return True


def _run(connection) -> None:  # type: ignore[no-untyped-def]
  context.configure(
    connection=connection, target_metadata=target_metadata, compare_type=True, include_object=_include_object
  )
  with context.begin_transaction():
    context.run_migrations()


async def _run_online() -> None:
  if not settings.database_url:
    raise RuntimeError("DATABASE_URL is not set.")
  engine = create_async_engine(settings.async_database_url)
  async with engine.connect() as connection:
    await connection.run_sync(_run)
  await engine.dispose()


if context.is_offline_mode():
  context.configure(url=settings.async_database_url, target_metadata=target_metadata, literal_binds=True)
  with context.begin_transaction():
    context.run_migrations()
else:
  asyncio.run(_run_online())
