"""Visual regression for `assertVisualMatch` steps: pixel-diff a screenshot
against the step's stored baseline. No baseline yet -> this screenshot
becomes it. A diff beyond the threshold fails the step and queues a pending
VisualDiff for human review -- baselines never change without an explicit
approval."""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Optional

from PIL import Image
from pixelmatch.contrib.PIL import pixelmatch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import services
from ..config import settings
from ..db import models as m
from ..db.base import new_id, utcnow


AREA = "visual-regression"


async def _put(relative: str, data: bytes) -> None:
  await services.get().artifacts.put(AREA, relative, data)  # type: ignore[union-attr]


async def _get(relative: str) -> bytes:
  data = await services.get().artifacts.get(AREA, relative)  # type: ignore[union-attr]
  if data is None:
    raise FileNotFoundError(f"Visual regression image missing: {relative}")
  return data


def _png(image: Image.Image) -> bytes:
  buffer = io.BytesIO()
  image.save(buffer, format="PNG")
  return buffer.getvalue()


@dataclass
class VisualOutcome:
  passed: bool
  note: str


async def check_visual_match(
  db: AsyncSession, test_step_id: str, test_run_result_id: str, png: bytes, threshold_override: Optional[float] = None
) -> VisualOutcome:
  actual = Image.open(io.BytesIO(png)).convert("RGBA")
  baseline_row = (
    await db.execute(select(m.VisualBaseline).where(m.VisualBaseline.test_step_id == test_step_id))
  ).scalar_one_or_none()

  if baseline_row is None:
    relative = f"baselines/{test_step_id}-baseline.png"
    await _put(relative, png)
    db.add(
      m.VisualBaseline(id=new_id(), test_step_id=test_step_id, file_path=relative, width=actual.width, height=actual.height)
    )
    await db.flush()
    return VisualOutcome(True, "No baseline existed yet -- this screenshot was saved as the new baseline.")

  baseline = Image.open(io.BytesIO(await _get(baseline_row.file_path))).convert("RGBA")
  threshold = threshold_override if threshold_override is not None else settings.visual_diff_threshold_percent
  actual_relative = f"diffs/{test_run_result_id}-actual.png"

  if baseline.size != actual.size:
    await _put(actual_relative, png)
    db.add(
      m.VisualDiff(
        id=new_id(),
        test_step_id=test_step_id,
        test_run_result_id=test_run_result_id,
        baseline_path=baseline_row.file_path,
        actual_path=actual_relative,
        diff_path=actual_relative,  # no pixel diff possible; show the actual image
        diff_percent=100.0,
        status="pending",
      )
    )
    await db.flush()
    return VisualOutcome(
      False,
      f"Screenshot dimensions ({actual.width}x{actual.height}) don't match the baseline "
      f"({baseline.width}x{baseline.height}) -- review in Visual Regression.",
    )

  diff_image = Image.new("RGBA", baseline.size)
  different = pixelmatch(baseline, actual, diff_image, threshold=0.1)
  diff_percent = different / (baseline.width * baseline.height) * 100
  if diff_percent <= threshold:
    return VisualOutcome(True, f"Visual match ({diff_percent:.3f}% of pixels differ, within the {threshold:g}% threshold).")

  diff_relative = f"diffs/{test_run_result_id}-diff.png"
  await _put(actual_relative, png)
  await _put(diff_relative, _png(diff_image))
  db.add(
    m.VisualDiff(
      id=new_id(),
      test_step_id=test_step_id,
      test_run_result_id=test_run_result_id,
      baseline_path=baseline_row.file_path,
      actual_path=actual_relative,
      diff_path=diff_relative,
      diff_percent=diff_percent,
      status="pending",
    )
  )
  await db.flush()
  return VisualOutcome(
    False,
    f"Visual difference detected: {diff_percent:.3f}% of pixels differ (threshold {threshold:g}%). Review in Visual Regression.",
  )


async def approve_diff(db: AsyncSession, diff: m.VisualDiff) -> None:
  """Promotes the diff's actual screenshot to the step's baseline."""
  data = await _get(diff.actual_path)
  image = Image.open(io.BytesIO(data))
  relative = f"baselines/{diff.test_step_id}-baseline.png"
  await _put(relative, data)
  baseline = (
    await db.execute(select(m.VisualBaseline).where(m.VisualBaseline.test_step_id == diff.test_step_id))
  ).scalar_one_or_none()
  if baseline is None:
    db.add(m.VisualBaseline(id=new_id(), test_step_id=diff.test_step_id, file_path=relative, width=image.width, height=image.height))
  else:
    baseline.file_path, baseline.width, baseline.height = relative, image.width, image.height
  diff.status = "approved"
  diff.resolved_at = utcnow()
  await db.flush()
