"""Filing a failed test run as an Azure DevOps Bug (POST /api/testruns/{id}/push-to/azureDevOps)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto import decrypt_secret
from ..db import models as m
from ..db.base import new_id, utcnow
from ..memory.learning import remember
from . import azure_devops


class NotConfigured(Exception):
  pass


@dataclass
class PushOutcome:
  push: m.IssuePush
  created: bool  # False when an earlier push already filed it


async def integration_for(db: AsyncSession, project_id: str) -> Optional[m.Integration]:
  return (
    await db.execute(
      select(m.Integration).where(m.Integration.project_id == project_id, m.Integration.type == azure_devops.INTEGRATION_TYPE)
    )
  ).scalar_one_or_none()


async def push_failed_run(db: AsyncSession, run_id: str) -> PushOutcome:
  """Idempotent: a run already pushed returns that push. A tracker-side
  failure is recorded on the push (status "failed"), not raised."""
  run = await db.get(m.TestRun, run_id)
  assert run is not None
  tc = await db.get(m.TestCase, run.test_case_id)
  assert tc is not None

  existing = (
    await db.execute(
      select(m.IssuePush)
      .where(m.IssuePush.test_run_id == run_id, m.IssuePush.type == azure_devops.INTEGRATION_TYPE)
      .order_by(m.IssuePush.created_at.desc())
      .limit(1)
    )
  ).scalar_one_or_none()
  if existing and existing.status == "pushed":
    return PushOutcome(existing, created=False)

  integration = await integration_for(db, tc.project_id)
  if integration is None:
    raise NotConfigured(
      f"{azure_devops.INTEGRATION_TYPE} integration not configured for this project. Configure it on the Integrations page first."
    )

  failed = (
    await db.execute(
      select(m.TestRunResult, m.TestStep.description)
      .join(m.TestStep, m.TestStep.id == m.TestRunResult.test_step_id)
      .where(m.TestRunResult.test_run_id == run_id, m.TestRunResult.status.in_(["failed", "error"]))
      .order_by(m.TestRunResult.step_order)
    )
  ).all()
  parts = [f"Objective: {tc.objective}"]
  for result, description in failed:
    lines = [f"Step: {description}"]
    if result.error_message:
      lines.append(f"Error: {result.error_message}")
    if result.suggested_fix:
      lines.append(f"Suggested fix: {result.suggested_fix}")
    parts.append("\n".join(lines))

  push = m.IssuePush(id=new_id(), test_run_id=run_id, type=azure_devops.INTEGRATION_TYPE, status="pending")
  db.add(push)
  await db.flush()
  try:
    issue = await azure_devops.create_bug(
      integration.base_url,
      integration.project_key,
      decrypt_secret(integration.api_token_encrypted),
      f"{tc.title} — failed run {run_id[-8:]}",
      "\n\n".join(parts),
    )
  except Exception as err:  # noqa: BLE001 -- recorded on the push
    push.status, push.error_message = "failed", str(err)
    await db.flush()
    return PushOutcome(push, created=False)
  push.status, push.issue_key, push.issue_url, push.pushed_at = "pushed", issue.key, issue.url, utcnow()
  await db.flush()
  remember(db, "defect", push.id)
  return PushOutcome(push, created=True)
