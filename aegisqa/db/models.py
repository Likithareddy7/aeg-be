"""SQLAlchemy models for the AegisQA schema.

Table and column names are the existing (camelCase) ones -- the schema was
first created by Prisma, and keeping it byte-for-byte identical means no data
migration. Python attributes are snake_case. tests/test_schema.py checks these
models against a migrated database so the two can't drift.

JSON-ish columns (TestCase.testData, TestStep.selector, ...) are TEXT holding
JSON strings, exactly as before.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, new_id, utcnow

Timestamp = TIMESTAMP(precision=3)
NOW = text("CURRENT_TIMESTAMP")


def _id() -> Mapped[str]:
  return mapped_column("id", Text, primary_key=True, default=new_id)


def _created_at(name: str = "createdAt") -> Mapped[datetime]:
  return mapped_column(name, Timestamp, nullable=False, server_default=NOW, default=utcnow)


def _updated_at() -> Mapped[datetime]:
  return mapped_column("updatedAt", Timestamp, nullable=False, default=utcnow, onupdate=utcnow)


def _fk(column: str, target: str, ondelete: str, nullable: bool = False) -> Mapped:
  return mapped_column(column, Text, ForeignKey(target, ondelete=ondelete, onupdate="CASCADE"), nullable=nullable)


class User(Base):
  __tablename__ = "User"
  __table_args__ = (Index("User_username_key", "username", unique=True),)

  id: Mapped[str] = _id()
  username: Mapped[str] = mapped_column("username", Text, nullable=False)
  password_hash: Mapped[str] = mapped_column("passwordHash", Text, nullable=False)
  role: Mapped[str] = mapped_column("role", Text, nullable=False, server_default=text("'member'"), default="member")
  created_at: Mapped[datetime] = _created_at()


class Session(Base):
  __tablename__ = "Session"
  __table_args__ = (Index("Session_userId_idx", "userId"),)

  id: Mapped[str] = mapped_column("id", Text, primary_key=True)
  user_id: Mapped[str] = _fk("userId", "User.id", "CASCADE")
  expires_at: Mapped[datetime] = mapped_column("expiresAt", Timestamp, nullable=False)
  created_at: Mapped[datetime] = _created_at()

  user: Mapped[User] = relationship(lazy="raise")


class Project(Base):
  __tablename__ = "Project"

  id: Mapped[str] = _id()
  name: Mapped[str] = mapped_column("name", Text, nullable=False)
  created_at: Mapped[datetime] = _created_at()

  urls: Mapped[list["TargetUrl"]] = relationship(lazy="raise", passive_deletes=True)


class AgentActivityEvent(Base):
  __tablename__ = "AgentActivityEvent"
  __table_args__ = (
    Index("AgentActivityEvent_projectId_userId_idx", "projectId", "userId"),
    Index("AgentActivityEvent_turnId_idx", "turnId"),
  )

  id: Mapped[str] = _id()
  project_id: Mapped[str] = _fk("projectId", "Project.id", "CASCADE")
  user_id: Mapped[str] = _fk("userId", "User.id", "CASCADE")
  turn_id: Mapped[str] = mapped_column("turnId", Text, nullable=False)
  stage: Mapped[str] = mapped_column("stage", Text, nullable=False)
  label: Mapped[str] = mapped_column("label", Text, nullable=False)
  detail: Mapped[Optional[str]] = mapped_column("detail", Text)
  tool_name: Mapped[Optional[str]] = mapped_column("toolName", Text)
  created_at: Mapped[datetime] = _created_at()


class ProjectMember(Base):
  __tablename__ = "ProjectMember"
  __table_args__ = (
    Index("ProjectMember_projectId_idx", "projectId"),
    Index("ProjectMember_userId_idx", "userId"),
    Index("ProjectMember_projectId_userId_key", "projectId", "userId", unique=True),
  )

  id: Mapped[str] = _id()
  project_id: Mapped[str] = _fk("projectId", "Project.id", "CASCADE")
  user_id: Mapped[str] = _fk("userId", "User.id", "CASCADE")
  role: Mapped[str] = mapped_column("role", Text, nullable=False, server_default=text("'editor'"), default="editor")
  created_at: Mapped[datetime] = _created_at()

  user: Mapped[User] = relationship(lazy="raise")


class AuditLogEntry(Base):
  __tablename__ = "AuditLogEntry"
  __table_args__ = (
    Index("AuditLogEntry_projectId_idx", "projectId"),
    Index("AuditLogEntry_userId_idx", "userId"),
    Index("AuditLogEntry_action_idx", "action"),
  )

  id: Mapped[str] = _id()
  user_id: Mapped[Optional[str]] = _fk("userId", "User.id", "SET NULL", nullable=True)
  project_id: Mapped[Optional[str]] = _fk("projectId", "Project.id", "SET NULL", nullable=True)
  action: Mapped[str] = mapped_column("action", Text, nullable=False)
  target_type: Mapped[Optional[str]] = mapped_column("targetType", Text)
  target_id: Mapped[Optional[str]] = mapped_column("targetId", Text)
  detail: Mapped[Optional[str]] = mapped_column("detail", Text)
  created_at: Mapped[datetime] = _created_at()

  user: Mapped[Optional[User]] = relationship(lazy="raise")


class Flow(Base):
  __tablename__ = "Flow"
  __table_args__ = (
    Index("Flow_projectId_idx", "projectId"),
    Index("Flow_projectId_name_key", "projectId", "name", unique=True),
  )

  id: Mapped[str] = _id()
  project_id: Mapped[str] = _fk("projectId", "Project.id", "CASCADE")
  name: Mapped[str] = mapped_column("name", Text, nullable=False)
  description: Mapped[Optional[str]] = mapped_column("description", Text)
  created_by_id: Mapped[Optional[str]] = _fk("createdById", "User.id", "SET NULL", nullable=True)
  created_at: Mapped[datetime] = _created_at()
  updated_at: Mapped[datetime] = _updated_at()

  created_by: Mapped[Optional[User]] = relationship(lazy="raise")
  versions: Mapped[list["FlowVersion"]] = relationship(lazy="raise", back_populates="flow", passive_deletes=True)


class FlowVersion(Base):
  __tablename__ = "FlowVersion"
  __table_args__ = (
    Index("FlowVersion_flowId_idx", "flowId"),
    Index("FlowVersion_flowId_version_key", "flowId", "version", unique=True),
  )

  id: Mapped[str] = _id()
  flow_id: Mapped[str] = _fk("flowId", "Flow.id", "CASCADE")
  version: Mapped[int] = mapped_column("version", Integer, nullable=False)
  steps: Mapped[str] = mapped_column("steps", Text, nullable=False)
  note: Mapped[Optional[str]] = mapped_column("note", Text)
  created_by_id: Mapped[Optional[str]] = _fk("createdById", "User.id", "SET NULL", nullable=True)
  created_at: Mapped[datetime] = _created_at()

  flow: Mapped[Flow] = relationship(lazy="raise", back_populates="versions")
  created_by: Mapped[Optional[User]] = relationship(lazy="raise")


class Environment(Base):
  __tablename__ = "Environment"
  __table_args__ = (Index("Environment_projectId_idx", "projectId"),)

  id: Mapped[str] = _id()
  project_id: Mapped[str] = _fk("projectId", "Project.id", "CASCADE")
  name: Mapped[str] = mapped_column("name", Text, nullable=False)
  base_url: Mapped[str] = mapped_column("baseUrl", Text, nullable=False)
  browser: Mapped[str] = mapped_column("browser", Text, nullable=False, server_default=text("'chromium'"), default="chromium")
  headless: Mapped[bool] = mapped_column("headless", Boolean, nullable=False, server_default=text("true"), default=True)
  viewport_width: Mapped[int] = mapped_column("viewportWidth", Integer, nullable=False, server_default=text("1280"), default=1280)
  viewport_height: Mapped[int] = mapped_column("viewportHeight", Integer, nullable=False, server_default=text("720"), default=720)
  credentials_encrypted: Mapped[Optional[str]] = mapped_column("credentialsEncrypted", Text)
  is_default: Mapped[bool] = mapped_column("isDefault", Boolean, nullable=False, server_default=text("false"), default=False)
  created_at: Mapped[datetime] = _created_at()
  updated_at: Mapped[datetime] = _updated_at()


class TargetUrl(Base):
  __tablename__ = "TargetUrl"
  __table_args__ = (Index("TargetUrl_projectId_idx", "projectId"),)

  id: Mapped[str] = _id()
  project_id: Mapped[str] = _fk("projectId", "Project.id", "CASCADE")
  url: Mapped[str] = mapped_column("url", Text, nullable=False)
  title: Mapped[Optional[str]] = mapped_column("title", Text)
  last_inspected_at: Mapped[Optional[datetime]] = mapped_column("lastInspectedAt", Timestamp)


class ChatMessage(Base):
  __tablename__ = "ChatMessage"
  __table_args__ = (Index("ChatMessage_projectId_userId_idx", "projectId", "userId"),)

  id: Mapped[str] = _id()
  project_id: Mapped[str] = _fk("projectId", "Project.id", "CASCADE")
  user_id: Mapped[str] = _fk("userId", "User.id", "CASCADE")
  role: Mapped[str] = mapped_column("role", Text, nullable=False)
  content: Mapped[str] = mapped_column("content", Text, nullable=False)
  created_at: Mapped[datetime] = _created_at()


class TestCase(Base):
  __tablename__ = "TestCase"
  __table_args__ = (
    Index("TestCase_projectId_idx", "projectId"),
    Index("TestCase_urlId_idx", "urlId"),
    Index("TestCase_createdById_idx", "createdById"),
  )

  id: Mapped[str] = _id()
  project_id: Mapped[str] = _fk("projectId", "Project.id", "CASCADE")
  url_id: Mapped[Optional[str]] = _fk("urlId", "TargetUrl.id", "SET NULL", nullable=True)
  title: Mapped[str] = mapped_column("title", Text, nullable=False)
  objective: Mapped[str] = mapped_column("objective", Text, nullable=False)
  preconditions: Mapped[str] = mapped_column("preconditions", Text, nullable=False)
  test_data: Mapped[str] = mapped_column("testData", Text, nullable=False)
  expected_result: Mapped[str] = mapped_column("expectedResult", Text, nullable=False)
  priority: Mapped[str] = mapped_column("priority", Text, nullable=False)
  type: Mapped[str] = mapped_column("type", Text, nullable=False)
  status: Mapped[str] = mapped_column("status", Text, nullable=False, server_default=text("'draft'"), default="draft")
  module: Mapped[Optional[str]] = mapped_column("module", Text)
  source_prompt: Mapped[Optional[str]] = mapped_column("sourcePrompt", Text)
  tags: Mapped[str] = mapped_column("tags", Text, nullable=False, server_default=text("'[]'"), default="[]")
  version: Mapped[int] = mapped_column("version", Integer, nullable=False, server_default=text("1"), default=1)
  created_by_id: Mapped[Optional[str]] = _fk("createdById", "User.id", "SET NULL", nullable=True)
  last_modified_by_id: Mapped[Optional[str]] = _fk("lastModifiedById", "User.id", "SET NULL", nullable=True)
  created_at: Mapped[datetime] = _created_at()
  updated_at: Mapped[datetime] = _updated_at()

  steps: Mapped[list["TestStep"]] = relationship(lazy="raise", passive_deletes=True, order_by="TestStep.order")
  url: Mapped[Optional[TargetUrl]] = relationship(lazy="raise")
  created_by: Mapped[Optional[User]] = relationship(lazy="raise", foreign_keys=[created_by_id])
  last_modified_by: Mapped[Optional[User]] = relationship(lazy="raise", foreign_keys=[last_modified_by_id])


class TestDataSet(Base):
  __tablename__ = "TestDataSet"
  __table_args__ = (Index("TestDataSet_testCaseId_key", "testCaseId", unique=True),)

  id: Mapped[str] = _id()
  test_case_id: Mapped[str] = _fk("testCaseId", "TestCase.id", "CASCADE")
  columns: Mapped[str] = mapped_column("columns", Text, nullable=False)
  secret_columns: Mapped[str] = mapped_column("secretColumns", Text, nullable=False)
  rows: Mapped[str] = mapped_column("rows", Text, nullable=False)
  source: Mapped[str] = mapped_column("source", Text, nullable=False)
  created_at: Mapped[datetime] = _created_at()
  updated_at: Mapped[datetime] = _updated_at()


class TestStep(Base):
  __tablename__ = "TestStep"
  __table_args__ = (
    Index("TestStep_testCaseId_idx", "testCaseId"),
    Index("TestStep_sourceFlowVersionId_idx", "sourceFlowVersionId"),
  )

  id: Mapped[str] = _id()
  test_case_id: Mapped[str] = _fk("testCaseId", "TestCase.id", "CASCADE")
  order: Mapped[int] = mapped_column("order", Integer, nullable=False)
  action: Mapped[str] = mapped_column("action", Text, nullable=False)
  selector: Mapped[Optional[str]] = mapped_column("selector", Text)
  locator_candidates: Mapped[Optional[str]] = mapped_column("locatorCandidates", Text)
  value: Mapped[Optional[str]] = mapped_column("value", Text)
  description: Mapped[str] = mapped_column("description", Text, nullable=False)
  enabled: Mapped[bool] = mapped_column("enabled", Boolean, nullable=False, server_default=text("true"), default=True)
  source_flow_version_id: Mapped[Optional[str]] = _fk("sourceFlowVersionId", "FlowVersion.id", "SET NULL", nullable=True)

  source_flow_version: Mapped[Optional[FlowVersion]] = relationship(lazy="raise")


class TestRun(Base):
  __tablename__ = "TestRun"
  __table_args__ = (
    Index("TestRun_testCaseId_idx", "testCaseId"),
    Index("TestRun_environmentId_idx", "environmentId"),
  )

  id: Mapped[str] = _id()
  test_case_id: Mapped[str] = _fk("testCaseId", "TestCase.id", "CASCADE")
  environment_id: Mapped[Optional[str]] = _fk("environmentId", "Environment.id", "SET NULL", nullable=True)
  status: Mapped[str] = mapped_column("status", Text, nullable=False)
  triggered_by: Mapped[str] = mapped_column("triggeredBy", Text, nullable=False, server_default=text("'user'"), default="user")
  batch_id: Mapped[Optional[str]] = mapped_column("batchId", Text)
  trace_path: Mapped[Optional[str]] = mapped_column("tracePath", Text)
  video_path: Mapped[Optional[str]] = mapped_column("videoPath", Text)
  continued_from_chat: Mapped[bool] = mapped_column(
    "continuedFromChat", Boolean, nullable=False, server_default=text("false"), default=False
  )
  resumed_from_run_id: Mapped[Optional[str]] = mapped_column("resumedFromRunId", Text)
  resumed_from_step_order: Mapped[Optional[int]] = mapped_column("resumedFromStepOrder", Integer)
  data_row_index: Mapped[Optional[int]] = mapped_column("dataRowIndex", Integer)
  started_at: Mapped[datetime] = _created_at("startedAt")
  finished_at: Mapped[Optional[datetime]] = mapped_column("finishedAt", Timestamp)


class TestRunResult(Base):
  __tablename__ = "TestRunResult"
  __table_args__ = (
    Index("TestRunResult_testRunId_idx", "testRunId"),
    Index("TestRunResult_testStepId_idx", "testStepId"),
  )

  id: Mapped[str] = _id()
  test_run_id: Mapped[str] = _fk("testRunId", "TestRun.id", "CASCADE")
  test_step_id: Mapped[str] = _fk("testStepId", "TestStep.id", "CASCADE")
  step_order: Mapped[int] = mapped_column("stepOrder", Integer, nullable=False)
  action: Mapped[Optional[str]] = mapped_column("action", Text)
  status: Mapped[str] = mapped_column("status", Text, nullable=False)
  actual_result: Mapped[Optional[str]] = mapped_column("actualResult", Text)
  error_message: Mapped[Optional[str]] = mapped_column("errorMessage", Text)
  suggested_fix: Mapped[Optional[str]] = mapped_column("suggestedFix", Text)
  recovered: Mapped[bool] = mapped_column("recovered", Boolean, nullable=False, server_default=text("false"), default=False)
  duration_ms: Mapped[int] = mapped_column("durationMs", Integer, nullable=False, server_default=text("0"), default=0)
  # Cookie/session data captured after a passing step -- server-side only, never returned by the API.
  storage_state_json: Mapped[Optional[str]] = mapped_column("storageStateJson", Text)
  page_url: Mapped[Optional[str]] = mapped_column("pageUrl", Text)


class VisualBaseline(Base):
  __tablename__ = "VisualBaseline"
  __table_args__ = (
    Index("VisualBaseline_testStepId_key", "testStepId", unique=True),
    Index("VisualBaseline_testStepId_idx", "testStepId"),
  )

  id: Mapped[str] = _id()
  test_step_id: Mapped[str] = _fk("testStepId", "TestStep.id", "CASCADE")
  file_path: Mapped[str] = mapped_column("filePath", Text, nullable=False)
  width: Mapped[int] = mapped_column("width", Integer, nullable=False)
  height: Mapped[int] = mapped_column("height", Integer, nullable=False)
  created_at: Mapped[datetime] = _created_at()
  updated_at: Mapped[datetime] = _updated_at()


class VisualDiff(Base):
  __tablename__ = "VisualDiff"
  __table_args__ = (
    Index("VisualDiff_testStepId_idx", "testStepId"),
    Index("VisualDiff_status_idx", "status"),
  )

  id: Mapped[str] = _id()
  test_step_id: Mapped[str] = _fk("testStepId", "TestStep.id", "CASCADE")
  test_run_result_id: Mapped[str] = _fk("testRunResultId", "TestRunResult.id", "CASCADE")
  baseline_path: Mapped[str] = mapped_column("baselinePath", Text, nullable=False)
  actual_path: Mapped[str] = mapped_column("actualPath", Text, nullable=False)
  diff_path: Mapped[str] = mapped_column("diffPath", Text, nullable=False)
  diff_percent: Mapped[float] = mapped_column("diffPercent", Float(precision=53), nullable=False)
  status: Mapped[str] = mapped_column("status", Text, nullable=False, server_default=text("'pending'"), default="pending")
  created_at: Mapped[datetime] = _created_at()
  resolved_at: Mapped[Optional[datetime]] = mapped_column("resolvedAt", Timestamp)


class HealingEvent(Base):
  __tablename__ = "HealingEvent"
  __table_args__ = (
    Index("HealingEvent_testStepId_idx", "testStepId"),
    Index("HealingEvent_testRunResultId_idx", "testRunResultId"),
  )

  id: Mapped[str] = _id()
  test_step_id: Mapped[str] = _fk("testStepId", "TestStep.id", "CASCADE")
  test_run_result_id: Mapped[str] = _fk("testRunResultId", "TestRunResult.id", "CASCADE")
  old_selector: Mapped[str] = mapped_column("oldSelector", Text, nullable=False)
  new_selector: Mapped[str] = mapped_column("newSelector", Text, nullable=False)
  confidence: Mapped[str] = mapped_column("confidence", Text, nullable=False)
  note: Mapped[str] = mapped_column("note", Text, nullable=False)
  # Historical name: holds the screenshot's file path.
  screenshot_id: Mapped[Optional[str]] = mapped_column("screenshotId", Text)
  approved: Mapped[bool] = mapped_column("approved", Boolean, nullable=False, server_default=text("false"), default=False)
  dismissed: Mapped[bool] = mapped_column("dismissed", Boolean, nullable=False, server_default=text("false"), default=False)
  created_at: Mapped[datetime] = _created_at()


class Screenshot(Base):
  __tablename__ = "Screenshot"
  __table_args__ = (Index("Screenshot_testRunResultId_idx", "testRunResultId"),)

  id: Mapped[str] = _id()
  test_run_result_id: Mapped[str] = _fk("testRunResultId", "TestRunResult.id", "CASCADE")
  step_order: Mapped[int] = mapped_column("stepOrder", Integer, nullable=False)
  file_path: Mapped[str] = mapped_column("filePath", Text, nullable=False)
  taken_at: Mapped[datetime] = _created_at("takenAt")


class TokenUsage(Base):
  __tablename__ = "TokenUsage"
  __table_args__ = (Index("TokenUsage_projectId_idx", "projectId"),)

  id: Mapped[str] = _id()
  project_id: Mapped[str] = _fk("projectId", "Project.id", "CASCADE")
  source: Mapped[str] = mapped_column("source", Text, nullable=False)
  model: Mapped[str] = mapped_column("model", Text, nullable=False)
  input_tokens: Mapped[int] = mapped_column("inputTokens", Integer, nullable=False)
  output_tokens: Mapped[int] = mapped_column("outputTokens", Integer, nullable=False)
  cache_read_tokens: Mapped[int] = mapped_column("cacheReadTokens", Integer, nullable=False)
  cache_write_tokens: Mapped[int] = mapped_column("cacheWriteTokens", Integer, nullable=False)
  cost_usd: Mapped[float] = mapped_column("costUsd", Float(precision=53), nullable=False)
  created_at: Mapped[datetime] = _created_at()


class ChangeRequest(Base):
  __tablename__ = "ChangeRequest"
  __table_args__ = (
    Index("ChangeRequest_testCaseId_idx", "testCaseId"),
    Index("ChangeRequest_status_idx", "status"),
  )

  id: Mapped[str] = _id()
  test_case_id: Mapped[str] = _fk("testCaseId", "TestCase.id", "CASCADE")
  requested_by_id: Mapped[str] = _fk("requestedById", "User.id", "CASCADE")
  note: Mapped[str] = mapped_column("note", Text, nullable=False)
  status: Mapped[str] = mapped_column("status", Text, nullable=False, server_default=text("'open'"), default="open")
  created_at: Mapped[datetime] = _created_at()
  resolved_at: Mapped[Optional[datetime]] = mapped_column("resolvedAt", Timestamp)

  requested_by: Mapped[User] = relationship(lazy="raise")
  test_case: Mapped[TestCase] = relationship(lazy="raise")


class ExecutionLog(Base):
  __tablename__ = "ExecutionLog"
  __table_args__ = (Index("ExecutionLog_testRunId_idx", "testRunId"),)

  id: Mapped[str] = _id()
  test_run_id: Mapped[str] = _fk("testRunId", "TestRun.id", "CASCADE")
  level: Mapped[str] = mapped_column("level", Text, nullable=False)
  category: Mapped[str] = mapped_column(
    "category", Text, nullable=False, server_default=text("'execution'"), default="execution"
  )
  message: Mapped[str] = mapped_column("message", Text, nullable=False)
  timestamp: Mapped[datetime] = _created_at("timestamp")


class Integration(Base):
  __tablename__ = "Integration"
  __table_args__ = (
    Index("Integration_projectId_idx", "projectId"),
    Index("Integration_projectId_type_key", "projectId", "type", unique=True),
  )

  id: Mapped[str] = _id()
  project_id: Mapped[str] = _fk("projectId", "Project.id", "CASCADE")
  type: Mapped[str] = mapped_column("type", Text, nullable=False)
  base_url: Mapped[str] = mapped_column("baseUrl", Text, nullable=False)
  email: Mapped[str] = mapped_column("email", Text, nullable=False)
  project_key: Mapped[str] = mapped_column("projectKey", Text, nullable=False)
  api_token_encrypted: Mapped[str] = mapped_column("apiTokenEncrypted", Text, nullable=False)
  created_at: Mapped[datetime] = _created_at()
  updated_at: Mapped[datetime] = _updated_at()


class IssuePush(Base):
  """One push of a failed run to an issue tracker. The physical table is
  still called JiraPush for historical reasons."""

  __tablename__ = "JiraPush"
  __table_args__ = (
    Index("JiraPush_testRunId_idx", "testRunId"),
    Index("JiraPush_status_idx", "status"),
  )

  id: Mapped[str] = _id()
  test_run_id: Mapped[str] = _fk("testRunId", "TestRun.id", "CASCADE")
  type: Mapped[str] = mapped_column("type", Text, nullable=False, server_default=text("'jira'"), default="jira")
  status: Mapped[str] = mapped_column("status", Text, nullable=False, server_default=text("'pending'"), default="pending")
  issue_key: Mapped[Optional[str]] = mapped_column("jiraIssueKey", Text)
  issue_url: Mapped[Optional[str]] = mapped_column("jiraIssueUrl", Text)
  error_message: Mapped[Optional[str]] = mapped_column("errorMessage", Text)
  created_at: Mapped[datetime] = _created_at()
  pushed_at: Mapped[Optional[datetime]] = mapped_column("pushedAt", Timestamp)


# ---------------------------------------------------------------------------
# Memory layer (exact store): everything the platform has learned, as one
# searchable record per item -- see aegisqa/memory/.
# ---------------------------------------------------------------------------

EMBEDDING_DIMENSIONS = 768


class MemoryRecord(Base):
  """One remembered item: a test case, a run result, an approved self-heal or
  a filed defect. `refId` points at the source row (no
  foreign key: kinds point at different tables, and memory may outlive them).
  `embedding` is null until an embedding model is available."""

  __tablename__ = "MemoryRecord"
  __table_args__ = (
    Index("MemoryRecord_projectId_kind_refId_key", "projectId", "kind", "refId", unique=True),
    Index("MemoryRecord_projectId_idx", "projectId"),
    Index("MemoryRecord_module_idx", "module"),
    Index("MemoryRecord_errorCode_idx", "errorCode"),
  )

  id: Mapped[str] = _id()
  project_id: Mapped[str] = _fk("projectId", "Project.id", "CASCADE")
  # "test_case" | "run_result" | "healing" | "defect"
  kind: Mapped[str] = mapped_column("kind", Text, nullable=False)
  ref_id: Mapped[str] = mapped_column("refId", Text, nullable=False)
  title: Mapped[str] = mapped_column("title", Text, nullable=False)
  content: Mapped[str] = mapped_column("content", Text, nullable=False)
  module: Mapped[Optional[str]] = mapped_column("module", Text)
  error_code: Mapped[Optional[str]] = mapped_column("errorCode", Text)
  metadata_: Mapped[str] = mapped_column("metadata", Text, nullable=False, server_default=text("'{}'"), default="{}")
  content_hash: Mapped[str] = mapped_column("contentHash", Text, nullable=False)
  embedding: Mapped[Optional[list[float]]] = mapped_column("embedding", Vector(EMBEDDING_DIMENSIONS))
  created_at: Mapped[datetime] = _created_at()
  updated_at: Mapped[datetime] = _updated_at()
