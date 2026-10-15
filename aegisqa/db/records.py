"""Small append-mostly records: audit log, token usage, chat history and the
agent activity timeline."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from . import models as m
from . import serializers as s
from .base import new_id

# ---------------------------------------------------------------------------
# Audit log -- who did what. Survives project/user deletion (SET NULL).
# ---------------------------------------------------------------------------


async def record_audit(
  db: AsyncSession,
  *,
  user_id: Optional[str],
  project_id: Optional[str],
  action: str,
  target_type: Optional[str] = None,
  target_id: Optional[str] = None,
  detail: Optional[str] = None,
) -> None:
  db.add(
    m.AuditLogEntry(
      id=new_id(),
      user_id=user_id,
      project_id=project_id,
      action=action,
      target_type=target_type,
      target_id=target_id,
      detail=detail,
    )
  )
  await db.flush()


async def list_audit(db: AsyncSession, project_id: str, limit: int = 200) -> list[dict[str, Any]]:
  rows = await db.execute(
    select(m.AuditLogEntry)
    .options(selectinload(m.AuditLogEntry.user))
    .where(m.AuditLogEntry.project_id == project_id)
    .order_by(m.AuditLogEntry.created_at.desc(), m.AuditLogEntry.id.desc())
    .limit(limit)
  )
  return [s.audit_entry(row) for row in rows.scalars()]


# ---------------------------------------------------------------------------
# Token usage. Per-million-token USD rates are placeholder estimates carried
# over from the original backend -- replace them with your contracted Vertex
# AI Gemini prices before using the cost figures for billing.
# ---------------------------------------------------------------------------

_RATES = {
  "opus": (15.0, 75.0, 18.75, 1.5),
  "sonnet": (3.0, 15.0, 3.75, 0.3),
  "haiku": (0.8, 4.0, 1.0, 0.08),
}
_DEFAULT_RATES = _RATES["sonnet"]


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
  lower = model.lower()
  rates = next((r for name, r in _RATES.items() if name in lower), _DEFAULT_RATES)
  rate_in, rate_out, rate_cache_write, rate_cache_read = rates
  cost = (
    input_tokens / 1e6 * rate_in
    + output_tokens / 1e6 * rate_out
    + cache_read / 1e6 * rate_cache_read
    + cache_write / 1e6 * rate_cache_write
  )
  return round(cost * 1_000_000) / 1_000_000


async def record_usage(db: AsyncSession, project_id: str, source: str, model: str, input_tokens: int, output_tokens: int) -> dict[str, Any]:
  row = m.TokenUsage(
    id=new_id(),
    project_id=project_id,
    source=source,
    model=model,
    input_tokens=input_tokens,
    output_tokens=output_tokens,
    cache_read_tokens=0,
    cache_write_tokens=0,
    cost_usd=cost_usd(model, input_tokens, output_tokens),
  )
  db.add(row)
  await db.flush()
  return s.token_usage(row)


async def usage_totals(db: AsyncSession, project_id: str) -> dict[str, Any]:
  row = (
    await db.execute(
      select(
        func.coalesce(func.sum(m.TokenUsage.input_tokens), 0),
        func.coalesce(func.sum(m.TokenUsage.output_tokens), 0),
        func.coalesce(func.sum(m.TokenUsage.cache_read_tokens), 0),
        func.coalesce(func.sum(m.TokenUsage.cache_write_tokens), 0),
        func.coalesce(func.sum(m.TokenUsage.cost_usd), 0.0),
      ).where(m.TokenUsage.project_id == project_id)
    )
  ).one()
  cost = float(row[4])
  return {
    "inputTokens": int(row[0]),
    "outputTokens": int(row[1]),
    "cacheReadTokens": int(row[2]),
    "cacheWriteTokens": int(row[3]),
    "costUsd": int(cost) if cost == int(cost) else cost,
  }


# ---------------------------------------------------------------------------
# Chat history and agent activity -- always private to one user on one project.
# ---------------------------------------------------------------------------


async def append_chat(db: AsyncSession, project_id: str, user_id: str, role: str, content: str) -> dict[str, Any]:
  row = m.ChatMessage(id=new_id(), project_id=project_id, user_id=user_id, role=role, content=content)
  db.add(row)
  await db.flush()
  return s.chat_message(row)


async def list_chat(db: AsyncSession, project_id: str, user_id: str) -> list[dict[str, Any]]:
  rows = await db.execute(
    select(m.ChatMessage)
    .where(m.ChatMessage.project_id == project_id, m.ChatMessage.user_id == user_id)
    .order_by(m.ChatMessage.created_at, m.ChatMessage.id)
  )
  return [s.chat_message(row) for row in rows.scalars()]


async def record_activity(
  db: AsyncSession,
  *,
  project_id: str,
  user_id: str,
  turn_id: str,
  stage: str,
  label: str,
  detail: Optional[str] = None,
  tool_name: Optional[str] = None,
) -> dict[str, Any]:
  row = m.AgentActivityEvent(
    id=new_id(),
    project_id=project_id,
    user_id=user_id,
    turn_id=turn_id,
    stage=stage,
    label=label,
    detail=detail,
    tool_name=tool_name,
  )
  db.add(row)
  await db.flush()
  return s.agent_activity(row)


async def list_activity(db: AsyncSession, project_id: str, user_id: str, limit: int = 200) -> list[dict[str, Any]]:
  rows = list(
    (
      await db.execute(
        select(m.AgentActivityEvent)
        .where(m.AgentActivityEvent.project_id == project_id, m.AgentActivityEvent.user_id == user_id)
        .order_by(m.AgentActivityEvent.created_at.desc())
        .limit(limit)
      )
    ).scalars()
  )
  return [s.agent_activity(row) for row in reversed(rows)]
