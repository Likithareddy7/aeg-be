"""Test runs as wire records."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import models as m
from . import serializers as s


async def result_records(db: AsyncSession, results: list[m.TestRunResult]) -> list[dict[str, Any]]:
  if not results:
    return []
  shots: dict[str, list[m.Screenshot]] = defaultdict(list)
  rows = await db.execute(select(m.Screenshot).where(m.Screenshot.test_run_result_id.in_([r.id for r in results])))
  for shot in rows.scalars():
    shots[shot.test_run_result_id].append(shot)
  return [s.test_run_result(r, shots[r.id]) for r in results]


async def records(db: AsyncSession, runs: list[m.TestRun], *, with_inherited: bool = False) -> list[dict[str, Any]]:
  if not runs:
    return []
  ids = [r.id for r in runs]
  results_by_run: dict[str, list[m.TestRunResult]] = defaultdict(list)
  all_results = list((await db.execute(select(m.TestRunResult).where(m.TestRunResult.test_run_id.in_(ids)))).scalars())
  for result in all_results:
    results_by_run[result.test_run_id].append(result)
  result_dicts = {r["id"]: r for r in await result_records(db, all_results)}

  logs_by_run: dict[str, list[m.ExecutionLog]] = defaultdict(list)
  for log in (await db.execute(select(m.ExecutionLog).where(m.ExecutionLog.test_run_id.in_(ids)))).scalars():
    logs_by_run[log.test_run_id].append(log)
  for logs in logs_by_run.values():
    logs.sort(key=lambda log: log.timestamp)

  pushes_by_run: dict[str, list[m.IssuePush]] = defaultdict(list)
  pushes = await db.execute(select(m.IssuePush).where(m.IssuePush.test_run_id.in_(ids)).order_by(m.IssuePush.created_at.desc()))
  for push in pushes.scalars():
    pushes_by_run[push.test_run_id].append(push)

  out = []
  for run in runs:
    inherited: Optional[list[dict[str, Any]]] = None
    if with_inherited and run.resumed_from_run_id and run.resumed_from_step_order:
      source = list(
        (
          await db.execute(
            select(m.TestRunResult)
            .where(
              m.TestRunResult.test_run_id == run.resumed_from_run_id,
              m.TestRunResult.step_order < run.resumed_from_step_order,
            )
            .order_by(m.TestRunResult.step_order)
          )
        ).scalars()
      )
      inherited = await result_records(db, source)
    out.append(
      s.test_run(
        run,
        [result_dicts[r.id] for r in results_by_run[run.id]],
        logs_by_run[run.id],
        pushes_by_run[run.id],
        inherited,
      )
    )
  return out


async def record(db: AsyncSession, run_id: str, *, with_inherited: bool = False) -> Optional[dict[str, Any]]:
  run = await db.get(m.TestRun, run_id)
  if run is None:
    return None
  return (await records(db, [run], with_inherited=with_inherited))[0]
