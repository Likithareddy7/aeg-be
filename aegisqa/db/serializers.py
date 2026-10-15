"""Database rows -> the JSON wire shapes the frontend consumes (the types in
the frontend repo's shared/src/index.ts). JSON-in-TEXT columns are parsed here."""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from . import models as m
from .base import iso, ms_between


def _json(value: Optional[str]) -> Any:
  return json.loads(value) if value else None


def user_summary(user: Optional[m.User]) -> Optional[dict[str, Any]]:
  if user is None:
    return None
  return {"id": user.id, "username": user.username, "role": "admin" if user.role == "admin" else "member"}


def target_url(url: m.TargetUrl) -> dict[str, Any]:
  return {
    "id": url.id,
    "projectId": url.project_id,
    "url": url.url,
    "title": url.title,
    "lastInspectedAt": iso(url.last_inspected_at),
  }


def project(row: m.Project, urls: Iterable[m.TargetUrl], my_role: Optional[str] = None) -> dict[str, Any]:
  out: dict[str, Any] = {
    "id": row.id,
    "name": row.name,
    "createdAt": iso(row.created_at),
    "urls": [target_url(u) for u in urls],
  }
  if my_role is not None:
    out["myRole"] = my_role
  return out


def chat_message(row: m.ChatMessage) -> dict[str, Any]:
  return {
    "id": row.id,
    "projectId": row.project_id,
    "role": row.role,
    "content": row.content,
    "createdAt": iso(row.created_at),
  }


def test_step(row: m.TestStep) -> dict[str, Any]:
  return {
    "id": row.id,
    "testCaseId": row.test_case_id,
    "order": row.order,
    "action": row.action,
    "selector": _json(row.selector),
    "locatorCandidates": _json(row.locator_candidates),
    "value": row.value,
    "description": row.description,
    "enabled": row.enabled,
  }


def test_case(
  row: m.TestCase,
  steps: Iterable[m.TestStep],
  created_by: Optional[m.User],
  last_modified_by: Optional[m.User],
  open_change_requests: int,
) -> dict[str, Any]:
  return {
    "id": row.id,
    "projectId": row.project_id,
    "urlId": row.url_id,
    "title": row.title,
    "objective": row.objective,
    "preconditions": row.preconditions,
    "testData": json.loads(row.test_data),
    "expectedResult": row.expected_result,
    "priority": row.priority,
    "type": row.type,
    "module": row.module,
    "status": row.status,
    "steps": [test_step(s) for s in sorted(steps, key=lambda s: s.order)],
    "createdBy": user_summary(created_by),
    "openChangeRequestCount": open_change_requests,
    "sourcePrompt": row.source_prompt,
    "tags": json.loads(row.tags),
    "version": row.version,
    "lastModifiedBy": user_summary(last_modified_by),
    "createdAt": iso(row.created_at),
    "updatedAt": iso(row.updated_at),
  }


def screenshot(row: m.Screenshot) -> dict[str, Any]:
  return {
    "id": row.id,
    "testRunResultId": row.test_run_result_id,
    "stepOrder": row.step_order,
    "filePath": row.file_path,
    "takenAt": iso(row.taken_at),
  }


def test_run_result(row: m.TestRunResult, screenshots: Iterable[m.Screenshot]) -> dict[str, Any]:
  return {
    "id": row.id,
    "testRunId": row.test_run_id,
    "testStepId": row.test_step_id,
    "stepOrder": row.step_order,
    "action": row.action,
    "status": row.status,
    "actualResult": row.actual_result,
    "errorMessage": row.error_message,
    "suggestedFix": row.suggested_fix,
    "recovered": row.recovered,
    "durationMs": row.duration_ms,
    "pageUrl": row.page_url,
    "screenshots": [screenshot(s) for s in screenshots],
  }


def token_usage(row: m.TokenUsage) -> dict[str, Any]:
  return {
    "id": row.id,
    "projectId": row.project_id,
    "source": row.source,
    "model": row.model,
    "inputTokens": row.input_tokens,
    "outputTokens": row.output_tokens,
    "cacheReadTokens": row.cache_read_tokens,
    "cacheWriteTokens": row.cache_write_tokens,
    "costUsd": row.cost_usd,
    "createdAt": iso(row.created_at),
  }


def execution_log(row: m.ExecutionLog) -> dict[str, Any]:
  return {
    "id": row.id,
    "testRunId": row.test_run_id,
    "level": row.level,
    "category": row.category,
    "message": row.message,
    "timestamp": iso(row.timestamp),
  }


def issue_push(row: m.IssuePush) -> dict[str, Any]:
  return {
    "id": row.id,
    "testRunId": row.test_run_id,
    "type": row.type,
    "status": row.status,
    "issueKey": row.issue_key,
    "issueUrl": row.issue_url,
    "errorMessage": row.error_message,
    "createdAt": iso(row.created_at),
    "pushedAt": iso(row.pushed_at),
  }


def test_run(
  row: m.TestRun,
  results: list[dict[str, Any]],
  logs: Iterable[m.ExecutionLog],
  pushes: Iterable[m.IssuePush] = (),
  inherited_results: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
  return {
    "id": row.id,
    "testCaseId": row.test_case_id,
    "environmentId": row.environment_id,
    "batchId": row.batch_id,
    "status": row.status,
    "triggeredBy": row.triggered_by,
    "startedAt": iso(row.started_at),
    "finishedAt": iso(row.finished_at),
    "tracePath": row.trace_path,
    "videoPath": row.video_path,
    "continuedFromChat": row.continued_from_chat,
    "resumedFromRunId": row.resumed_from_run_id,
    "resumedFromStepOrder": row.resumed_from_step_order,
    "dataRowIndex": row.data_row_index,
    "inheritedResults": inherited_results or [],
    "results": sorted(results, key=lambda r: r["stepOrder"]),
    "logs": [execution_log(log) for log in logs],
    "issuePushes": [issue_push(p) for p in pushes],
  }


def environment(row: m.Environment) -> dict[str, Any]:
  return {
    "id": row.id,
    "projectId": row.project_id,
    "name": row.name,
    "baseUrl": row.base_url,
    "browser": row.browser,
    "headless": row.headless,
    "viewportWidth": row.viewport_width,
    "viewportHeight": row.viewport_height,
    "isDefault": row.is_default,
    "hasCredentials": row.credentials_encrypted is not None,
    "createdAt": iso(row.created_at),
    "updatedAt": iso(row.updated_at),
  }


def visual_diff(row: m.VisualDiff) -> dict[str, Any]:
  return {
    "id": row.id,
    "testStepId": row.test_step_id,
    "testRunResultId": row.test_run_result_id,
    "baselinePath": row.baseline_path,
    "actualPath": row.actual_path,
    "diffPath": row.diff_path,
    "diffPercent": row.diff_percent,
    "status": row.status,
    "createdAt": iso(row.created_at),
    "resolvedAt": iso(row.resolved_at),
  }


def healing_event(row: m.HealingEvent) -> dict[str, Any]:
  return {
    "id": row.id,
    "testStepId": row.test_step_id,
    "testRunResultId": row.test_run_result_id,
    "oldSelector": json.loads(row.old_selector),
    "newSelector": json.loads(row.new_selector),
    "confidence": row.confidence,
    "note": row.note,
    "screenshotPath": row.screenshot_id,
    "approved": row.approved,
    "dismissed": row.dismissed,
    "createdAt": iso(row.created_at),
  }


def member(row: m.ProjectMember) -> dict[str, Any]:
  return {
    "id": row.id,
    "projectId": row.project_id,
    "user": user_summary(row.user),
    "role": row.role,
    "createdAt": iso(row.created_at),
  }


def audit_entry(row: m.AuditLogEntry) -> dict[str, Any]:
  return {
    "id": row.id,
    "userId": row.user_id,
    "username": row.user.username if row.user else None,
    "projectId": row.project_id,
    "action": row.action,
    "targetType": row.target_type,
    "targetId": row.target_id,
    "detail": row.detail,
    "createdAt": iso(row.created_at),
  }


def agent_activity(row: m.AgentActivityEvent) -> dict[str, Any]:
  return {
    "id": row.id,
    "projectId": row.project_id,
    "turnId": row.turn_id,
    "stage": row.stage,
    "label": row.label,
    "detail": row.detail,
    "toolName": row.tool_name,
    "createdAt": iso(row.created_at),
  }


def change_request(row: m.ChangeRequest, test_case_title: str, requested_by: m.User) -> dict[str, Any]:
  return {
    "id": row.id,
    "testCaseId": row.test_case_id,
    "testCaseTitle": test_case_title,
    "requestedBy": user_summary(requested_by),
    "note": row.note,
    "status": "resolved" if row.status == "resolved" else "open",
    "createdAt": iso(row.created_at),
    "resolvedAt": iso(row.resolved_at),
  }


def test_data_set(row: m.TestDataSet) -> dict[str, Any]:
  return {
    "id": row.id,
    "testCaseId": row.test_case_id,
    "columns": json.loads(row.columns),
    "secretColumns": json.loads(row.secret_columns),
    "rows": json.loads(row.rows),
    "source": row.source,
    "createdAt": iso(row.created_at),
    "updatedAt": iso(row.updated_at),
  }


def flow_version(row: m.FlowVersion, created_by: Optional[m.User]) -> dict[str, Any]:
  return {
    "id": row.id,
    "flowId": row.flow_id,
    "version": row.version,
    "steps": json.loads(row.steps),
    "note": row.note,
    "createdBy": user_summary(created_by),
    "createdAt": iso(row.created_at),
  }


def integration(row: m.Integration) -> dict[str, Any]:
  return {
    "id": row.id,
    "projectId": row.project_id,
    "type": row.type,
    "baseUrl": row.base_url,
    "email": row.email,
    "projectKey": row.project_key,
    "hasApiToken": len(row.api_token_encrypted) > 0,
    "createdAt": iso(row.created_at),
    "updatedAt": iso(row.updated_at),
  }


def run_duration_ms(row: m.TestRun) -> int:
  return max(0, ms_between(row.started_at, row.finished_at)) if row.finished_at else 0
