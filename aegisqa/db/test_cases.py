"""Test cases: loading them as wire records, and every way they change."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Iterable, Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import models as m
from . import serializers as s
from ..memory.learning import remember
from .base import new_id, utcnow


async def _users(db: AsyncSession, ids: Iterable[Optional[str]]) -> dict[str, m.User]:
  wanted = {i for i in ids if i}
  if not wanted:
    return {}
  rows = (await db.execute(select(m.User).where(m.User.id.in_(wanted)))).scalars()
  return {u.id: u for u in rows}


async def records(
  db: AsyncSession, test_cases: list[m.TestCase], *, with_provenance: bool = False
) -> list[dict[str, Any]]:
  """Serializes test cases with their steps, authors and open change-request
  counts in a fixed number of queries. `with_provenance` adds each step's
  `sourceFlow` (which flow/version it was inserted from, and whether that
  version is still the latest)."""
  if not test_cases:
    return []
  ids = [tc.id for tc in test_cases]

  steps_by_case: dict[str, list[m.TestStep]] = defaultdict(list)
  for step in (await db.execute(select(m.TestStep).where(m.TestStep.test_case_id.in_(ids)))).scalars():
    steps_by_case[step.test_case_id].append(step)

  users = await _users(db, [u for tc in test_cases for u in (tc.created_by_id, tc.last_modified_by_id)])

  open_counts = dict(
    (
      await db.execute(
        select(m.ChangeRequest.test_case_id, func.count())
        .where(m.ChangeRequest.test_case_id.in_(ids), m.ChangeRequest.status == "open")
        .group_by(m.ChangeRequest.test_case_id)
      )
    ).all()
  )

  out = [
    s.test_case(
      tc,
      steps_by_case[tc.id],
      users.get(tc.created_by_id or ""),
      users.get(tc.last_modified_by_id or ""),
      int(open_counts.get(tc.id, 0)),
    )
    for tc in test_cases
  ]

  if with_provenance:
    version_ids = {st.source_flow_version_id for sts in steps_by_case.values() for st in sts if st.source_flow_version_id}
    if version_ids:
      rows = (
        await db.execute(
          select(m.FlowVersion, m.Flow).join(m.Flow, m.Flow.id == m.FlowVersion.flow_id).where(m.FlowVersion.id.in_(version_ids))
        )
      ).all()
      latest = dict(
        (
          await db.execute(
            select(m.FlowVersion.flow_id, func.max(m.FlowVersion.version))
            .where(m.FlowVersion.flow_id.in_({flow.id for _, flow in rows}))
            .group_by(m.FlowVersion.flow_id)
          )
        ).all()
      )
      by_version = {version.id: (version, flow) for version, flow in rows}
      source_by_step = {st.id: st.source_flow_version_id for sts in steps_by_case.values() for st in sts}
      for record in out:
        for step in record["steps"]:
          entry = by_version.get(source_by_step.get(step["id"]) or "")
          if entry:
            version, flow = entry
            step["sourceFlow"] = {
              "flowId": flow.id,
              "flowName": flow.name,
              "versionId": version.id,
              "version": version.version,
              "isLatest": version.version == latest.get(flow.id),
            }
  return out


async def record(db: AsyncSession, test_case_id: str, *, with_provenance: bool = False) -> Optional[dict[str, Any]]:
  tc = await db.get(m.TestCase, test_case_id)
  if tc is None:
    return None
  return (await records(db, [tc], with_provenance=with_provenance))[0]


def _step_row(test_case_id: str, step: dict[str, Any], order: Optional[int] = None, source: Optional[str] = None) -> m.TestStep:
  return m.TestStep(
    id=new_id(),
    test_case_id=test_case_id,
    order=order if order is not None else step["order"],
    action=step["action"],
    selector=json.dumps(step["selector"]) if step.get("selector") else None,
    locator_candidates=json.dumps(step["locatorCandidates"]) if step.get("locatorCandidates") else None,
    value=step.get("value"),
    description=step["description"],
    enabled=step.get("enabled", True) if step.get("enabled") is not None else True,
    source_flow_version_id=source if source is not None else step.get("sourceFlowVersionId"),
  )


async def create_proposed(
  db: AsyncSession,
  project_id: str,
  url_id: Optional[str],
  test_cases: list[dict[str, Any]],
  created_by_id: Optional[str],
  source_prompt: Optional[str],
) -> list[dict[str, Any]]:
  created: list[m.TestCase] = []
  for tc in test_cases:
    row = m.TestCase(
      id=new_id(),
      project_id=project_id,
      url_id=url_id,
      created_by_id=created_by_id,
      last_modified_by_id=created_by_id,
      source_prompt=source_prompt,
      title=tc["title"],
      objective=tc["objective"],
      preconditions=tc["preconditions"],
      test_data=json.dumps(tc["testData"]),
      expected_result=tc["expectedResult"],
      priority=tc["priority"],
      type=tc["type"],
      module=tc.get("module"),
      status="draft",
    )
    db.add(row)
    await db.flush()
    for step in tc["steps"]:
      # Proposed steps never carry flow provenance.
      db.add(_step_row(row.id, {**step, "sourceFlowVersionId": None}))
    remember(db, "test_case", row.id)
    created.append(row)
  await db.flush()
  return await records(db, created)


_UPDATABLE = {
  "title": "title",
  "objective": "objective",
  "preconditions": "preconditions",
  "expectedResult": "expected_result",
  "priority": "priority",
  "type": "type",
  "module": "module",
}


async def apply_update(
  db: AsyncSession, project_id: str, test_case_id: str, updates: dict[str, Any], acting_user_id: Optional[str]
) -> Optional[dict[str, Any]]:
  """Applies only the fields present in `updates`; `steps`, if present,
  replaces the whole step list. Bumps `version`."""
  tc = (
    await db.execute(select(m.TestCase).where(m.TestCase.id == test_case_id, m.TestCase.project_id == project_id))
  ).scalar_one_or_none()
  if tc is None:
    return None
  for key, attr in _UPDATABLE.items():
    if key in updates:
      setattr(tc, attr, updates[key])
  if "testData" in updates:
    tc.test_data = json.dumps(updates["testData"])
  if "tags" in updates:
    tc.tags = json.dumps(updates["tags"])
  tc.version = tc.version + 1
  tc.updated_at = utcnow()
  if acting_user_id:
    tc.last_modified_by_id = acting_user_id

  if updates.get("steps") is not None:
    await db.execute(delete(m.TestStep).where(m.TestStep.test_case_id == test_case_id))
    for step in updates["steps"]:
      db.add(_step_row(test_case_id, step))
  await db.flush()
  remember(db, "test_case", test_case_id)
  return await record(db, test_case_id)


async def set_status(db: AsyncSession, test_case_id: str, status: str) -> None:
  await db.execute(update(m.TestCase).where(m.TestCase.id == test_case_id).values(status=status, updated_at=utcnow()))


async def insert_flow(db: AsyncSession, test_case_id: str, flow_version_id: str) -> Optional[dict[str, Any]]:
  """Appends a flow version's steps to the end of a test case, tagged with
  their source version for provenance."""
  if await db.get(m.TestCase, test_case_id) is None:
    return None
  version = await db.get(m.FlowVersion, flow_version_id)
  if version is None:
    raise ValueError("Flow version not found")
  max_order = (
    await db.execute(select(func.coalesce(func.max(m.TestStep.order), 0)).where(m.TestStep.test_case_id == test_case_id))
  ).scalar_one()
  for i, step in enumerate(json.loads(version.steps)):
    db.add(_step_row(test_case_id, step, order=max_order + i + 1, source=flow_version_id))
  await db.flush()
  remember(db, "test_case", test_case_id)
  return await record(db, test_case_id)


async def update_flow_block(
  db: AsyncSession, test_case_id: str, source_flow_version_id: str, to_flow_version_id: str
) -> Optional[dict[str, Any]]:
  """Replaces every step inserted from `source_flow_version_id` with
  `to_flow_version_id`'s steps, at the position of the first replaced step.
  Only ever runs on an explicit "update to latest" click."""
  if await db.get(m.TestCase, test_case_id) is None:
    return None
  to_version = await db.get(m.FlowVersion, to_flow_version_id)
  if to_version is None:
    raise ValueError("Target flow version not found")

  all_steps = list(
    (await db.execute(select(m.TestStep).where(m.TestStep.test_case_id == test_case_id).order_by(m.TestStep.order))).scalars()
  )
  matched = [st for st in all_steps if st.source_flow_version_id == source_flow_version_id]
  if not matched:
    raise ValueError("No steps in this test case came from that flow version.")
  remaining = [st for st in all_steps if st.source_flow_version_id != source_flow_version_id]
  first_matched = min(st.order for st in matched)
  insertion = len([st for st in remaining if st.order < first_matched])

  def copy(st: m.TestStep, order: int) -> m.TestStep:
    return m.TestStep(
      id=new_id(),
      test_case_id=test_case_id,
      order=order,
      action=st.action,
      selector=st.selector,
      locator_candidates=st.locator_candidates,
      value=st.value,
      description=st.description,
      enabled=st.enabled,
      source_flow_version_id=st.source_flow_version_id,
    )

  await db.execute(delete(m.TestStep).where(m.TestStep.test_case_id == test_case_id))
  order = 1
  for st in remaining[:insertion]:
    db.add(copy(st, order))
    order += 1
  for step in json.loads(to_version.steps):
    db.add(_step_row(test_case_id, step, order=order, source=to_flow_version_id))
    order += 1
  for st in remaining[insertion:]:
    db.add(copy(st, order))
    order += 1
  await db.flush()
  remember(db, "test_case", test_case_id)
  return await record(db, test_case_id)
