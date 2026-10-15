"""Memory layer (exact store): the MemoryRecord table, with a pgvector
embedding column, a full-text index and an HNSW similarity index.

Needs the `vector` extension -- available on Cloud SQL for PostgreSQL; locally
`brew install pgvector`.

Revision ID: 0002_memory
Revises: 0001_baseline
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0002_memory"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.execute("CREATE EXTENSION IF NOT EXISTS vector")
  op.create_table(
    "MemoryRecord",
    sa.Column("id", sa.Text(), nullable=False),
    sa.Column("projectId", sa.Text(), nullable=False),
    sa.Column("kind", sa.Text(), nullable=False),
    sa.Column("refId", sa.Text(), nullable=False),
    sa.Column("title", sa.Text(), nullable=False),
    sa.Column("content", sa.Text(), nullable=False),
    sa.Column("module", sa.Text(), nullable=True),
    sa.Column("errorCode", sa.Text(), nullable=True),
    sa.Column("metadata", sa.Text(), server_default=sa.text("'{}'"), nullable=False),
    sa.Column("contentHash", sa.Text(), nullable=False),
    sa.Column("embedding", Vector(768), nullable=True),
    sa.Column("createdAt", postgresql.TIMESTAMP(precision=3), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    sa.Column("updatedAt", postgresql.TIMESTAMP(precision=3), nullable=False),
    sa.ForeignKeyConstraint(["projectId"], ["Project.id"], onupdate="CASCADE", ondelete="CASCADE"),
    sa.PrimaryKeyConstraint("id"),
  )
  op.create_index("MemoryRecord_projectId_kind_refId_key", "MemoryRecord", ["projectId", "kind", "refId"], unique=True)
  op.create_index("MemoryRecord_projectId_idx", "MemoryRecord", ["projectId"], unique=False)
  op.create_index("MemoryRecord_module_idx", "MemoryRecord", ["module"], unique=False)
  op.create_index("MemoryRecord_errorCode_idx", "MemoryRecord", ["errorCode"], unique=False)
  # Not modelled in SQLAlchemy (expression / access-method indexes); see
  # migrations/env.py, which keeps autogenerate from proposing to drop them.
  op.execute(
    'CREATE INDEX "MemoryRecord_fulltext_idx" ON "MemoryRecord" '
    "USING gin (to_tsvector('english', title || ' ' || content))"
  )
  op.execute('CREATE INDEX "MemoryRecord_embedding_idx" ON "MemoryRecord" USING hnsw (embedding vector_cosine_ops)')


def downgrade() -> None:
  op.drop_table("MemoryRecord")
