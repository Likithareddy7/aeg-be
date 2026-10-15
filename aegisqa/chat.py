"""The platform side of the agents: running a chat turn (persist the
conversation, relay the agent's events to the UI, record token usage, keep
the activity timeline), the two platform tools the agent saves work with,
and the one-shot agent calls the test runner uses."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Optional

from pydantic import ValidationError
from sqlalchemy import select

from . import services
from .agents.definitions import build_failure_analysis_agent, build_self_healing_agent, build_step_builder_agent
from .agents.browser import mcp_result_to_text
from .agents.prompts import failure_analysis_prompt, self_healing_prompt, step_builder_prompt
from .agents.runtime import ChatTurnRequest, Usage, run_chat_turn, run_structured
from .agents.schemas import FailureExplanation, GeneratedStep, ProposeTestCasesIn, SelectorRecovery, UpdateTestCaseIn
from .config import settings
from .db import models as m
from .db import records, test_cases
from .db.base import session_scope
from .errors import describe_validation_error
from .ws.registry import Emit, chat_emit

logger = logging.getLogger(__name__)


def chat_session_key(project_id: str, user_id: str) -> str:
  """Each user gets their own isolated chat browser per project."""
  return f"chat:{project_id}:{user_id}"


# ---------------------------------------------------------------------------
# Platform tools (propose_test_cases / update_test_case)
# ---------------------------------------------------------------------------


async def run_platform_tool(name: str, args: dict[str, Any], context: dict[str, Any], emit: Emit) -> dict[str, Any]:
  project_id, user_id = context["projectId"], context["userId"]
  try:
    async with session_scope() as db:
      if name == "propose_test_cases":
        payload = ProposeTestCasesIn.model_validate(args)
        created = await test_cases.create_proposed(
          db,
          project_id,
          context.get("urlId"),
          [tc.to_dict() for tc in payload.testCases],
          user_id,
          context.get("userText") or None,
        )
        emit({"type": "testcases.proposed", "testCases": created})
        return {"text": f"Saved {len(created)} test case(s): {', '.join(c['title'] for c in created)}.", "isError": False}

      if name == "update_test_case":
        payload = UpdateTestCaseIn.model_validate(args)
        existing = await db.get(m.TestCase, payload.testCaseId)
        if existing and existing.created_by_id and existing.created_by_id != user_id:
          owner = await db.get(m.User, existing.created_by_id)
          return {
            "text": f"Can't edit this test case: it's owned by {owner.username if owner else 'another user'}. "
            'Use the "request changes" option on the test case instead of editing it directly.',
            "isError": True,
          }
        updated = await test_cases.apply_update(db, project_id, payload.testCaseId, payload.updates.to_dict(), user_id)
        if updated is None:
          return {"text": f"No test case found with id {payload.testCaseId}.", "isError": True}
        emit({"type": "testcases.updated", "testCase": updated})
        return {"text": f'Updated test case "{updated["title"]}": {payload.summary}', "isError": False}

      return {"text": f"Unknown tool {name}", "isError": True}
  except ValidationError as err:
    return {"text": f"Failed to apply {name}: {describe_validation_error(err)}", "isError": True}
  except Exception as err:  # noqa: BLE001 -- reported to the model as a tool error
    logger.exception("platform tool %s failed", name)
    return {"text": f"Failed to apply {name}: {err}", "isError": True}


class InProcessPlatformClient:
  def __init__(self, emit: Emit):
    self._emit = emit

  async def call_tool(self, name: str, context: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    return await run_platform_tool(name, args, context, self._emit)


# ---------------------------------------------------------------------------
# Chat turns
# ---------------------------------------------------------------------------

_active_turns: dict[str, asyncio.Task[None]] = {}


def cancel_chat_turn(turn_id: str) -> bool:
  task = _active_turns.get(turn_id)
  if task is None:
    return False
  task.cancel()
  return True


async def _activity(emit: Emit, project_id: str, user_id: str, turn_id: str, stage: str, label: str, **extra: Any) -> None:
  async with session_scope() as db:
    event = await records.record_activity(
      db, project_id=project_id, user_id=user_id, turn_id=turn_id, stage=stage, label=label, **extra
    )
  emit({"type": "agent.activity", "event": event})


async def run_chat_turn_streaming(
  project_id: str,
  url_id: Optional[str],
  target_url: str,
  user_text: str,
  user_id: str,
  emit: Emit,
  environment_id: Optional[str] = None,
) -> None:
  """One full chat turn: the test generation agent explores the page and
  proposes test cases, streamed to the user as it goes. Every stage is
  persisted as an AgentActivityEvent so a page refresh shows the same
  timeline."""
  turn_id = str(uuid.uuid4())
  task = asyncio.current_task()
  if task is not None:
    _active_turns[turn_id] = task
  svc = services.get()
  try:
    async with session_scope() as db:
      prior = await records.list_chat(db, project_id, user_id)
      user_message = await records.append_chat(db, project_id, user_id, "user", user_text)
      browser_config: Optional[dict[str, Any]] = None
      if environment_id:
        env = await db.get(m.Environment, environment_id)
        if env is not None:
          browser_config = {
            "browser": env.browser,
            "headless": env.headless,
            "viewportWidth": env.viewport_width,
            "viewportHeight": env.viewport_height,
          }
    emit({"type": "chat.message", "message": user_message})
    await _activity(emit, project_id, user_id, turn_id, "queued", "Prompt received")

    request = ChatTurnRequest(
      turnId=turn_id,
      projectId=project_id,
      userId=user_id,
      urlId=url_id,
      targetUrl=target_url,
      userText=user_text,
      history=[{"role": msg["role"], "content": msg["content"]} for msg in prior],
      browserSessionKey=chat_session_key(project_id, user_id),
      browserConfig=browser_config,
    )
    async for event in run_chat_turn(
      request,
      model=svc.model,
      browsers=svc.browsers,
      platform_client=InProcessPlatformClient(chat_emit(project_id, user_id)),
      max_llm_calls=settings.max_llm_calls_per_turn,
    ):
      kind = event["type"]
      if kind == "activity":
        await _activity(
          emit, project_id, user_id, turn_id, event["stage"], event["label"], tool_name=event.get("toolName"), detail=event.get("detail")
        )
      elif kind == "delta":
        emit({"type": "chat.delta", "text": event["text"], "turnId": turn_id})
      elif kind == "usage":
        async with session_scope() as db:
          usage = await records.record_usage(db, project_id, "chat", event["model"], event["inputTokens"], event["outputTokens"])
          totals = await records.usage_totals(db, project_id)
        emit({"type": "usage.update", "usage": usage, "totals": totals})
      elif kind == "error":
        label = "Couldn't start the browser session" if event["code"] == "browser_unavailable" else "Agent error"
        await _activity(emit, project_id, user_id, turn_id, "failed", label, detail=event["message"])
        emit({"type": "error", "message": event["message"]})
      elif kind == "done":
        if event["text"]:
          async with session_scope() as db:
            saved = await records.append_chat(db, project_id, user_id, "assistant", event["text"])
          emit({"type": "chat.message", "message": saved})
        # No tool call at all means the agent just replied (usually a
        # clarifying question): that's "waiting for you", not "done".
        if not event["usedAnyTool"]:
          await _activity(emit, project_id, user_id, turn_id, "waiting_for_user", "Waiting for your input")
        else:
          label = "Test cases ready" if event["producedTestCases"] else "Done"
          await _activity(emit, project_id, user_id, turn_id, "completed", label)
  except asyncio.CancelledError:
    await _activity(emit, project_id, user_id, turn_id, "cancelled", "Cancelled")
  except Exception as err:  # noqa: BLE001
    logger.exception("chat turn failed")
    message = f"Agent error: {err}"
    await _activity(emit, project_id, user_id, turn_id, "failed", "Agent error", detail=message)
    emit({"type": "error", "message": message})
  finally:
    _active_turns.pop(turn_id, None)


# ---------------------------------------------------------------------------
# One-shot agents used by the test runner and the step editor
# ---------------------------------------------------------------------------


async def _record(project_id: str, source: str, usage: Usage) -> None:
  async with session_scope() as db:
    await records.record_usage(db, project_id, source, services.get().model_name, usage.input_tokens, usage.output_tokens)


async def explain_failure(project_id: str, step_description: str, action: str, error_message: str, page_snapshot: str) -> FailureExplanation:
  parsed, raw, usage = await run_structured(
    build_failure_analysis_agent(services.get().model),
    failure_analysis_prompt(step_description, action, error_message, page_snapshot),
    FailureExplanation,
  )
  await _record(project_id, "failure_explanation", usage)
  return parsed or FailureExplanation(
    explanation=raw, suggestedFix="Review the step manually -- could not parse a structured suggestion."
  )


async def recover_selector(project_id: str, role: Optional[str], name: Optional[str], description: str, page_snapshot: str) -> SelectorRecovery:
  parsed, _, usage = await run_structured(
    build_self_healing_agent(services.get().model),
    self_healing_prompt(role, name, description, page_snapshot),
    SelectorRecovery,
  )
  await _record(project_id, "selector_recovery", usage)
  return parsed or SelectorRecovery(
    ref=None, confidence="low", note="Could not parse recovery response.", matchedRole=None, matchedName=None
  )


async def generate_step(project_id: str, target_url: str, instruction: str) -> dict[str, Any]:
  """One plain-English instruction -> one step, grounded in a fresh snapshot
  of the page. Returned for review, never saved."""
  browsers = services.get().browsers
  key = f"stepgen:{project_id}:{uuid.uuid4().hex}"
  snapshot = "(no snapshot available)"
  try:
    browser = await browsers.get(key)
    await browser.call_tool("browser_navigate", {"url": target_url})
    snapshot = mcp_result_to_text(await browser.call_tool("browser_snapshot"))
  except Exception as err:  # noqa: BLE001 -- generation still runs without a snapshot
    logger.warning("step generation: page inspection failed: %s", err)
  finally:
    await browsers.close(key)

  parsed, _, usage = await run_structured(
    build_step_builder_agent(services.get().model), step_builder_prompt(instruction, snapshot), GeneratedStep
  )
  await _record(project_id, "step_generation", usage)
  step = parsed or GeneratedStep(action="waitFor", selector=None, value="1000", description="Wait briefly")
  return {**step.model_dump(), "order": 0}


async def latest_target_url(project_id: str, url_id: Optional[str]) -> Optional[m.TargetUrl]:
  async with session_scope() as db:
    if url_id:
      return await db.get(m.TargetUrl, url_id)
    return (
      await db.execute(
        select(m.TargetUrl)
        .where(m.TargetUrl.project_id == project_id)
        .order_by(m.TargetUrl.last_inspected_at.desc().nulls_last())
        .limit(1)
      )
    ).scalar_one_or_none()
