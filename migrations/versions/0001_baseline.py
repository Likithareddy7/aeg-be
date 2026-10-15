"""Baseline: the full AegisQA schema.

The SQL is the exact schema the original (Prisma-managed) backend created, so
a database created by either one is identical. A database that already has
these tables only needs `alembic stamp 0001_baseline`.

Revision ID: 0001_baseline
"""

from pathlib import Path

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
  sql = (Path(__file__).with_suffix(".sql")).read_text("utf-8")
  for statement in sql.split(";"):
    if statement.strip() and not all(line.strip().startswith("--") or not line.strip() for line in statement.splitlines()):
      op.execute(statement)


def downgrade() -> None:
  raise NotImplementedError("The baseline can't be downgraded.")
