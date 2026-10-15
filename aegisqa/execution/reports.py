"""Downloadable batch reports: JUnit XML (for CI) and a self-contained HTML
page. One entry per test run."""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Optional

from ..db.base import iso, utcnow


@dataclass
class BatchRun:
  id: str
  test_case_title: str
  status: str
  started_at: str
  finished_at: Optional[str]
  duration_ms: int
  error_message: Optional[str]


def _xml(text: str) -> str:
  return (
    text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;")
  )


def junit_xml(batch_id: str, runs: list[BatchRun]) -> str:
  failures = sum(1 for r in runs if r.status == "failed")
  errors = sum(1 for r in runs if r.status == "error")
  skipped = sum(1 for r in runs if r.status == "skipped")
  total = sum(r.duration_ms for r in runs) / 1000
  suite = _xml(batch_id)
  cases = []
  for run in runs:
    attrs = f'classname="{suite}" name="{_xml(run.test_case_title)}" time="{run.duration_ms / 1000:.3f}"'
    if run.status == "failed":
      message = run.error_message or "Test failed"
      cases.append(f'  <testcase {attrs}>\n    <failure message="{_xml(message)}">{_xml(run.error_message or "")}</failure>\n  </testcase>')
    elif run.status == "error":
      message = run.error_message or "Test errored"
      cases.append(f'  <testcase {attrs}>\n    <error message="{_xml(message)}">{_xml(run.error_message or "")}</error>\n  </testcase>')
    elif run.status == "skipped":
      cases.append(f"  <testcase {attrs}>\n    <skipped/>\n  </testcase>")
    else:
      cases.append(f"  <testcase {attrs}/>")
  return (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    f'<testsuite name="{suite}" tests="{len(runs)}" failures="{failures}" errors="{errors}" skipped="{skipped}" time="{total:.3f}">\n'
    + "\n".join(cases)
    + "\n</testsuite>\n"
  )


_STATUS_COLOR = {
  "passed": "#22c55e",
  "failed": "#ef4444",
  "error": "#ef4444",
  "skipped": "#94a3b8",
  "running": "#4f8cff",
  "pending": "#94a3b8",
}


def _h(text: str) -> str:
  return html.escape(text, quote=True).replace("&#x27;", "'")


def html_report(batch_id: str, runs: list[BatchRun]) -> str:
  passed = sum(1 for r in runs if r.status == "passed")
  failed = sum(1 for r in runs if r.status in ("failed", "error"))
  skipped = sum(1 for r in runs if r.status == "skipped")
  rows = "\n".join(
    f"""      <tr>
        <td>{_h(run.test_case_title)}</td>
        <td style="color:{_STATUS_COLOR.get(run.status, "#94a3b8")};font-weight:600;">{_h(run.status)}</td>
        <td>{run.duration_ms / 1000:.2f}s</td>
        <td>{_h(run.started_at)}</td>
        <td>{_h(run.finished_at or "-")}</td>
        <td>{_h(run.error_message[:300]) if run.error_message else "-"}</td>
      </tr>"""
    for run in runs
  )
  return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>AegisQA CI report -- {_h(batch_id)}</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; background: #0b1020; color: #f8fafc; margin: 0; padding: 32px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .meta {{ color: #94a3b8; font-size: 13px; margin-bottom: 20px; }}
  .counts {{ display: flex; gap: 16px; margin-bottom: 24px; }}
  .count {{ background: #172033; border: 1px solid #263248; border-radius: 10px; padding: 12px 18px; }}
  .count .n {{ font-size: 22px; font-weight: 700; }}
  .count .l {{ font-size: 12px; color: #94a3b8; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #263248; }}
  th {{ color: #94a3b8; font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.03em; }}
</style>
</head>
<body>
  <h1>AegisQA CI report</h1>
  <div class="meta">Batch <code>{_h(batch_id)}</code> &middot; generated {_h(iso(utcnow()) or "")}</div>
  <div class="counts">
    <div class="count"><div class="n">{len(runs)}</div><div class="l">Total</div></div>
    <div class="count"><div class="n" style="color:#22c55e;">{passed}</div><div class="l">Passed</div></div>
    <div class="count"><div class="n" style="color:#ef4444;">{failed}</div><div class="l">Failed</div></div>
    <div class="count"><div class="n" style="color:#94a3b8;">{skipped}</div><div class="l">Skipped</div></div>
  </div>
  <table>
    <thead>
      <tr><th>Test case</th><th>Status</th><th>Duration</th><th>Started</th><th>Finished</th><th>Error</th></tr>
    </thead>
    <tbody>
{rows}
    </tbody>
  </table>
</body>
</html>
"""
