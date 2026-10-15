"""Dashboard, failing tests, reports and the activity feed -- all computed on
read from existing rows; there are no analytics tables."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from . import models as m
from . import serializers as s
from .base import iso, ms_between, utcnow

WINDOW_SIZE = 20
FLAKY_LOOKBACK_RUNS = 5
RECENT_ACTIVITY_LIMIT = 8
FAILING_TESTS_LIMIT = 10
PRIORITY_SEVERITY = {"critical": 3, "high": 2, "medium": 1, "low": 0}

# "Time saved" is a projection, not a measurement: an assumed manual pass per
# automated run, at an assumed blended QA rate. Tune to your team's baseline.
ASSUMED_MANUAL_MINUTES_PER_TEST = 12
ASSUMED_HOURLY_RATE_USD = 75


def _round_half_up(value: float) -> int:
  """JavaScript's Math.round (Python's round() uses banker's rounding)."""
  import math

  return math.floor(value + 0.5)


def estimate_time_saved(run_count: int, automated_duration_ms: int) -> tuple[float, int]:
  manual_minutes = run_count * ASSUMED_MANUAL_MINUTES_PER_TEST
  hours = max(0.0, (manual_minutes - automated_duration_ms / 60_000) / 60)
  return _round_half_up(hours * 10) / 10, _round_half_up(hours * ASSUMED_HOURLY_RATE_USD)


def _summarize(statuses: list[str]) -> dict[str, Any]:
  passed = sum(1 for st in statuses if st == "passed")
  failed = sum(1 for st in statuses if st in ("failed", "error"))
  skipped = sum(1 for st in statuses if st == "skipped")
  return {
    "passCount": passed,
    "failCount": failed,
    "skipCount": skipped,
    "passRate": passed / len(statuses) if statuses else 0,
  }


def _health(pass_rate: float, flaky_ratio: float) -> int:
  return max(0, min(100, _round_half_up((pass_rate * 0.7 + (1 - flaky_ratio) * 0.3) * 100)))


async def latest_status_by_test_case(db: AsyncSession, test_case_ids: list[str]) -> tuple[dict[str, str], list[str]]:
  """Each test case's latest run status, and which are flaky (a pass and a
  fail/error among their last FLAKY_LOOKBACK_RUNS runs)."""
  if not test_case_ids:
    return {}, []
  ranked = (
    select(
      m.TestRun.test_case_id,
      m.TestRun.status,
      func.row_number().over(partition_by=m.TestRun.test_case_id, order_by=m.TestRun.started_at.desc()).label("rn"),
    )
    .where(m.TestRun.test_case_id.in_(test_case_ids))
    .subquery()
  )
  rows = (await db.execute(select(ranked.c.test_case_id, ranked.c.status, ranked.c.rn).where(ranked.c.rn <= FLAKY_LOOKBACK_RUNS))).all()
  latest: dict[str, str] = {}
  recent: dict[str, set[str]] = defaultdict(set)
  for test_case_id, status, rn in rows:
    if rn == 1:
      latest[test_case_id] = status
    recent[test_case_id].add(status)
  flaky = [
    tc_id
    for tc_id in test_case_ids
    if "passed" in recent[tc_id] and ("failed" in recent[tc_id] or "error" in recent[tc_id])
  ]
  return latest, flaky


async def open_change_requests(db: AsyncSession, project_id: str, limit: Optional[int] = None) -> list[dict[str, Any]]:
  query = (
    select(m.ChangeRequest, m.TestCase.title, m.User)
    .join(m.TestCase, m.TestCase.id == m.ChangeRequest.test_case_id)
    .join(m.User, m.User.id == m.ChangeRequest.requested_by_id)
    .where(m.ChangeRequest.status == "open", m.TestCase.project_id == project_id)
    .order_by(m.ChangeRequest.created_at.desc())
  )
  if limit:
    query = query.limit(limit)
  return [s.change_request(cr, title, user) for cr, title, user in (await db.execute(query)).all()]


async def activity_feed(db: AsyncSession, project_id: str, limit: int = RECENT_ACTIVITY_LIMIT) -> list[dict[str, Any]]:
  """Recent generations, run completions, self-heals and change requests,
  newest first."""
  per_source = 15
  items: list[dict[str, Any]] = []

  for tc_id, title, created_at in (
    await db.execute(
      select(m.TestCase.id, m.TestCase.title, m.TestCase.created_at)
      .where(m.TestCase.project_id == project_id)
      .order_by(m.TestCase.created_at.desc())
      .limit(per_source)
    )
  ).all():
    items.append(
      {"kind": "generated", "message": f"Test case created: {title}", "timestamp": iso(created_at), "href": f"/project/{project_id}/tests/{tc_id}"}
    )

  for status, finished_at, title in (
    await db.execute(
      select(m.TestRun.status, m.TestRun.finished_at, m.TestCase.title)
      .join(m.TestCase, m.TestCase.id == m.TestRun.test_case_id)
      .where(m.TestCase.project_id == project_id, m.TestRun.finished_at.is_not(None))
      .order_by(m.TestRun.finished_at.desc())
      .limit(per_source)
    )
  ).all():
    items.append({"kind": "run_completed", "message": f"Run {status}: {title}", "timestamp": iso(finished_at)})

  for created_at, title in (
    await db.execute(
      select(m.HealingEvent.created_at, m.TestCase.title)
      .join(m.TestStep, m.TestStep.id == m.HealingEvent.test_step_id)
      .join(m.TestCase, m.TestCase.id == m.TestStep.test_case_id)
      .where(m.TestCase.project_id == project_id)
      .order_by(m.HealingEvent.created_at.desc())
      .limit(per_source)
    )
  ).all():
    items.append(
      {"kind": "healed", "message": f"Self-healed a selector in {title}", "timestamp": iso(created_at), "href": f"/project/{project_id}/healing"}
    )

  for created_at, note, tc_id, username, title in (
    await db.execute(
      select(m.ChangeRequest.created_at, m.ChangeRequest.note, m.ChangeRequest.test_case_id, m.User.username, m.TestCase.title)
      .join(m.TestCase, m.TestCase.id == m.ChangeRequest.test_case_id)
      .join(m.User, m.User.id == m.ChangeRequest.requested_by_id)
      .where(m.TestCase.project_id == project_id)
      .order_by(m.ChangeRequest.created_at.desc())
      .limit(per_source)
    )
  ).all():
    items.append(
      {
        "kind": "change_requested",
        "message": f"{username} flagged {title}",
        "detail": note,
        "timestamp": iso(created_at),
        "href": f"/project/{project_id}/tests/{tc_id}",
      }
    )

  items.sort(key=lambda item: item["timestamp"], reverse=True)
  return items[:limit]


async def dashboard(db: AsyncSession, project_id: str) -> dict[str, Any]:
  test_cases = (
    await db.execute(
      select(m.TestCase.id, m.TestCase.title, m.TestCase.status, m.TestCase.type, m.TestCase.priority, m.TestCase.module).where(
        m.TestCase.project_id == project_id
      )
    )
  ).all()

  by_status: dict[str, int] = {}
  by_type: dict[str, int] = {}
  for tc in test_cases:
    by_status[tc.status] = by_status.get(tc.status, 0) + 1
    by_type[tc.type] = by_type.get(tc.type, 0) + 1

  # Two windows of runs: the newest WINDOW_SIZE are "current", the next
  # WINDOW_SIZE "previous" (used only for the trend delta).
  run_rows = (
    await db.execute(
      select(m.TestRun, m.TestCase.title)
      .join(m.TestCase, m.TestCase.id == m.TestRun.test_case_id)
      .where(m.TestCase.project_id == project_id)
      .order_by(m.TestRun.started_at.desc())
      .limit(WINDOW_SIZE * 2)
    )
  ).all()
  all_runs = [
    {
      "id": run.id,
      "testCaseId": run.test_case_id,
      "testCaseTitle": title,
      "status": run.status,
      "startedAt": iso(run.started_at),
      "finishedAt": iso(run.finished_at),
      "durationMs": s.run_duration_ms(run),
    }
    for run, title in run_rows
  ]
  recent_runs = all_runs[:WINDOW_SIZE]
  previous_runs = all_runs[WINDOW_SIZE:]
  current = _summarize([r["status"] for r in recent_runs])
  previous = _summarize([r["status"] for r in previous_runs]) if previous_runs else None

  durations = [r["durationMs"] for r in recent_runs if r["durationMs"] > 0]
  average_duration = _round_half_up(sum(durations) / len(durations)) if durations else 0

  latest, flaky_all = await latest_status_by_test_case(db, [tc.id for tc in test_cases])
  approved = [tc for tc in test_cases if tc.status == "approved"]
  approved_ids = {tc.id for tc in approved}
  flaky_approved = [tc_id for tc_id in flaky_all if tc_id in approved_ids]
  passing_latest = sum(1 for tc in approved if latest.get(tc.id) == "passed")
  release_readiness = (
    0 if not approved else max(0, _round_half_up(passing_latest / len(approved) * 100 - len(flaky_approved) * 5))
  )

  flaky_ratio = len(flaky_all) / len(test_cases) if test_cases else 0
  health = _health(current["passRate"], flaky_ratio)
  health_delta = health - _health(previous["passRate"], flaky_ratio) if previous else None
  coverage = _round_half_up(by_status.get("approved", 0) / len(test_cases) * 100) if test_cases else 0

  failing_by_priority = {"critical": 0, "high": 0, "medium": 0, "low": 0}
  failing: list[dict[str, Any]] = []
  for tc in test_cases:
    if latest.get(tc.id) in ("failed", "error"):
      failing_by_priority[tc.priority] = failing_by_priority.get(tc.priority, 0) + 1
      failing.append({"id": tc.id, "title": tc.title, "priority": tc.priority})
  failing.sort(key=lambda f: PRIORITY_SEVERITY.get(f["priority"], 0), reverse=True)

  groups: dict[str, dict[str, int]] = {}
  for tc in test_cases:
    name = (tc.module or "").strip() or "Unassigned"
    group = groups.setdefault(name, {"testCount": 0, "ranCount": 0, "passedCount": 0})
    group["testCount"] += 1
    if tc.id in latest:
      group["ranCount"] += 1
      if latest[tc.id] == "passed":
        group["passedCount"] += 1
  coverage_by_module = sorted(
    (
      {
        "module": name,
        "testCount": g["testCount"],
        "passRate": _round_half_up(g["passedCount"] / g["ranCount"] * 100) if g["ranCount"] else None,
      }
      for name, g in groups.items()
    ),
    key=lambda c: c["testCount"],
    reverse=True,
  )

  finished = (
    await db.execute(
      select(m.TestRun.started_at, m.TestRun.finished_at)
      .join(m.TestCase, m.TestCase.id == m.TestRun.test_case_id)
      .where(m.TestCase.project_id == project_id, m.TestRun.finished_at.is_not(None))
    )
  ).all()
  automated_ms = sum(max(0, ms_between(start, end)) for start, end in finished)
  hours, usd = estimate_time_saved(len(finished), automated_ms)

  healed = (
    await db.execute(
      select(func.count())
      .select_from(m.HealingEvent)
      .join(m.TestStep, m.TestStep.id == m.HealingEvent.test_step_id)
      .join(m.TestCase, m.TestCase.id == m.TestStep.test_case_id)
      .where(m.TestCase.project_id == project_id)
    )
  ).scalar_one()

  open_requests = await open_change_requests(db, project_id)

  return {
    "totalTestCases": len(test_cases),
    "testCasesByStatus": by_status,
    "testCasesByType": by_type,
    "recentRuns": recent_runs,
    "passCount": current["passCount"],
    "failCount": current["failCount"],
    "skipCount": current["skipCount"],
    "flakyTestCaseIds": flaky_approved,
    "averageDurationMs": average_duration,
    "averageDurationSampleSize": len(durations),
    "releaseReadinessScore": release_readiness,
    "openChangeRequestCount": len(open_requests),
    "openChangeRequests": open_requests[:10],
    "testHealthScore": health,
    "automationCoveragePercent": coverage,
    "timeSavedHours": hours,
    "timeSavedUsd": usd,
    "healedLocatorCount": healed,
    "maxParallelRuns": settings.max_parallel_runs,
    "trend": {"testHealthDelta": health_delta, "automationCoverageDelta": None, "releaseReadinessDelta": None},
    "failingTestsByPriority": failing_by_priority,
    "failingTests": failing[:FAILING_TESTS_LIMIT],
    "coverageByModule": coverage_by_module,
    "recentActivity": await activity_feed(db, project_id),
  }


async def failing_tests(db: AsyncSession, project_id: str) -> list[dict[str, Any]]:
  """Every test case whose latest run failed/errored, most severe first."""
  test_cases = (
    await db.execute(
      select(m.TestCase.id, m.TestCase.title, m.TestCase.priority, m.TestCase.module).where(m.TestCase.project_id == project_id)
    )
  ).all()
  latest, _ = await latest_status_by_test_case(db, [tc.id for tc in test_cases])
  out = []
  for tc in test_cases:
    if latest.get(tc.id) not in ("failed", "error"):
      continue
    run = (
      await db.execute(select(m.TestRun).where(m.TestRun.test_case_id == tc.id).order_by(m.TestRun.started_at.desc()).limit(1))
    ).scalar_one_or_none()
    latest_run = None
    if run is not None:
      error = (
        await db.execute(
          select(m.TestRunResult.error_message)
          .where(m.TestRunResult.test_run_id == run.id, m.TestRunResult.error_message.is_not(None))
          .order_by(m.TestRunResult.step_order)
          .limit(1)
        )
      ).scalar_one_or_none()
      latest_run = {"id": run.id, "status": run.status, "errorMessage": error, "finishedAt": iso(run.finished_at)}
    out.append({"id": tc.id, "title": tc.title, "priority": tc.priority, "module": tc.module, "latestRun": latest_run})
  out.sort(key=lambda f: PRIORITY_SEVERITY.get(f["priority"], 0), reverse=True)
  return out


async def runs_time_series(db: AsyncSession, project_id: str, days: int = 30) -> list[dict[str, Any]]:
  """Runs in the last `days` days grouped by UTC calendar day, every day
  present (zeros included) so charts have a continuous axis."""
  now = utcnow()
  since = now - timedelta(days=days)
  runs = (
    await db.execute(
      select(m.TestRun.status, m.TestRun.started_at, m.TestRun.finished_at)
      .join(m.TestCase, m.TestCase.id == m.TestRun.test_case_id)
      .where(m.TestCase.project_id == project_id, m.TestRun.started_at >= since)
    )
  ).all()
  buckets: dict[str, dict[str, Any]] = {}
  for status, started_at, finished_at in runs:
    bucket = buckets.setdefault(started_at.strftime("%Y-%m-%d"), {"passed": 0, "failed": 0, "skipped": 0, "durations": []})
    if status == "passed":
      bucket["passed"] += 1
    elif status in ("failed", "error"):
      bucket["failed"] += 1
    elif status == "skipped":
      bucket["skipped"] += 1
    if finished_at:
      bucket["durations"].append(max(0, ms_between(started_at, finished_at)))
  points = []
  for offset in range(days - 1, -1, -1):
    date = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
    bucket = buckets.get(date)
    durations = bucket["durations"] if bucket else []
    points.append(
      {
        "date": date,
        "passed": bucket["passed"] if bucket else 0,
        "failed": bucket["failed"] if bucket else 0,
        "skipped": bucket["skipped"] if bucket else 0,
        "avgDurationMs": _round_half_up(sum(durations) / len(durations)) if durations else 0,
      }
    )
  return points
