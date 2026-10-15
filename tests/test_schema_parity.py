"""The SQLAlchemy models must describe exactly the schema the migrations
create -- otherwise the next `alembic revision --autogenerate` would pick up
phantom changes (or miss real ones)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.skipif(
  not TEST_DATABASE_URL or "test" not in TEST_DATABASE_URL.rsplit("/", 1)[-1],
  reason="set TEST_DATABASE_URL to a database whose name contains 'test'",
)


def test_models_match_migrations() -> None:
  import psycopg
  from alembic.autogenerate import compare_metadata
  from alembic.migration import MigrationContext
  from sqlalchemy import create_engine

  from aegisqa.db import models  # noqa: F401
  from aegisqa.db.base import Base

  with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")
  subprocess.run(
    [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=Path(__file__).resolve().parents[1], check=True, capture_output=True
  )
  engine = create_engine(TEST_DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1))
  def include(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    # Expression / HNSW indexes created by hand in 0004_memory (as in migrations/env.py).
    return not (type_ == "index" and name in ("MemoryRecord_fulltext_idx", "MemoryRecord_embedding_idx"))

  with engine.connect() as conn:
    context = MigrationContext.configure(conn, opts={"compare_type": True, "include_object": include})
    diff = compare_metadata(context, Base.metadata)
  engine.dispose()
  assert diff == []
