"""Runs the ADK agents and turns their event streams into the small, stable
event vocabulary the chat relay (aegisqa/chat.py) consumes."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional, TypeVar, Union

from google.adk.agents import LlmAgent
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.events import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, ValidationError

from .definitions import build_test_generation_agent
from .browser import BrowserConfig, BrowserSessionManager
from .platform_tools import (
  PLATFORM_TOOL_NAMES,
  STATE_PROJECT_ID,
  STATE_TARGET_URL,
  STATE_URL_ID,
  STATE_USER_ID,
  STATE_USER_TEXT,
  PlatformClient,
  build_platform_tools,
)

logger = logging.getLogger(__name__)

APP_NAME = "aegisqa"

ModelT = TypeVar("ModelT", bound=BaseModel)


@dataclass
class Usage:
  input_tokens: int = 0
  output_tokens: int = 0

  def add(self, metadata: Optional[types.GenerateContentResponseUsageMetadata]) -> None:
    if metadata is None:
      return
    self.input_tokens += metadata.prompt_token_count or 0
    self.output_tokens += (metadata.candidates_token_count or 0) + (metadata.thoughts_token_count or 0)

  def as_dict(self) -> dict[str, int]:
    return {"inputTokens": self.input_tokens, "outputTokens": self.output_tokens}


def model_name(model: Union[str, BaseLlm]) -> str:
  return model if isinstance(model, str) else model.model


def _text_of(event: Event) -> str:
  if not event.content or not event.content.parts:
    return ""
  return "".join(part.text or "" for part in event.content.parts if part.text and not part.thought)


def describe_browser_tool(name: str) -> str:
  if name == "search_project_memory":
    return "Checking what the project already knows..."
  if "navigate" in name:
    return "Navigating the browser..."
  if "snapshot" in name:
    return "Inspecting the page..."
  if "screenshot" in name:
    return "Taking a screenshot..."
  if "click" in name:
    return "Clicking an element..."
  if "type" in name or "fill" in name:
    return "Typing into a field..."
  return f"Running {name}..."


# ---------------------------------------------------------------------------
# Chat turn (test generation agent)
# ---------------------------------------------------------------------------


class ChatTurnRequest(BaseModel):
  turnId: str
  projectId: str
  userId: str
  urlId: Optional[str] = None
  targetUrl: str
  userText: str
  # Prior conversation, oldest first, NOT including userText.
  history: list[dict[str, str]] = []
  browserSessionKey: str
  browserConfig: Optional[dict[str, Any]] = None


async def run_chat_turn(
  request: ChatTurnRequest,
  *,
  model: Union[str, BaseLlm],
  browsers: BrowserSessionManager,
  platform_client: PlatformClient,
  max_llm_calls: int,
) -> AsyncIterator[dict[str, Any]]:
  """Yields events:
    {"type": "activity", "stage", "label", "toolName"?}
    {"type": "delta", "text"}
    {"type": "usage", "model", "inputTokens", "outputTokens"}   (one per model call)
    {"type": "error", "code", "message"}                         (terminal)
    {"type": "done", "text", "usedAnyTool", "producedTestCases"} (terminal)
  """
  cfg = request.browserConfig or {}
  try:
    browser = await browsers.get(
      request.browserSessionKey,
      BrowserConfig(
        browser=cfg.get("browser"),
        headless=cfg.get("headless"),
        viewport_width=cfg.get("viewportWidth"),
        viewport_height=cfg.get("viewportHeight"),
      ),
    )
    # Starts the browser (if needed) and fails fast if Playwright MCP is unreachable.
    await browser.get_tools()
  except Exception as err:  # noqa: BLE001
    logger.exception("chat: browser session unavailable")
    await browsers.close(request.browserSessionKey)
    yield {
      "type": "error",
      "code": "browser_unavailable",
      "message": (
        "Could not start the browser automation session (Playwright MCP). "
        "Is `npx @playwright/mcp` reachable, and have you run `npx playwright install`? "
        f"({err})"
      ),
    }
    return

  agent = build_test_generation_agent(model, browser, build_platform_tools(platform_client))
  sessions = InMemorySessionService()
  session = await sessions.create_session(
    app_name=APP_NAME,
    user_id=request.userId,
    state={
      STATE_TARGET_URL: request.targetUrl,
      STATE_PROJECT_ID: request.projectId,
      STATE_USER_ID: request.userId,
      STATE_URL_ID: request.urlId,
      STATE_USER_TEXT: request.userText,
    },
  )
  # The database is the source of truth for chat history; replay it into
  # this turn's ADK session.
  for message in request.history:
    is_user = message.get("role") == "user"
    await sessions.append_event(
      session,
      Event(
        invocation_id=f"history-{uuid.uuid4().hex}",
        author="user" if is_user else agent.name,
        content=types.Content(role="user" if is_user else "model", parts=[types.Part(text=message.get("content", ""))]),
      ),
    )

  runner = Runner(agent=agent, app_name=APP_NAME, session_service=sessions)
  yield {"type": "activity", "stage": "understanding", "label": "Understanding the test objective"}

  assistant_text = ""
  used_any_tool = False
  produced_test_cases = False
  streamed_this_call = False
  last_partial_usage: Optional[types.GenerateContentResponseUsageMetadata] = None
  name = model_name(model)

  try:
    async for event in runner.run_async(
      user_id=request.userId,
      session_id=session.id,
      new_message=types.Content(role="user", parts=[types.Part(text=request.userText)]),
      run_config=RunConfig(streaming_mode=StreamingMode.SSE, max_llm_calls=max_llm_calls),
    ):
      if event.author == "user":
        continue

      if event.partial:
        delta = _text_of(event)
        if delta:
          streamed_this_call = True
          assistant_text += delta
          yield {"type": "delta", "text": delta}
        last_partial_usage = event.usage_metadata or last_partial_usage
        continue

      responses = event.get_function_responses()
      if responses:
        for response in responses:
          if response.name == "propose_test_cases" and not (response.response or {}).get("isError"):
            produced_test_cases = True
        continue

      # A complete model response (one per LLM call).
      if not streamed_this_call:
        text = _text_of(event)
        if text:
          assistant_text += text
          yield {"type": "delta", "text": text}
      streamed_this_call = False

      usage = Usage()
      usage.add(event.usage_metadata or last_partial_usage)
      last_partial_usage = None
      if event.usage_metadata is not None or usage.input_tokens or usage.output_tokens:
        yield {"type": "usage", "model": name, **usage.as_dict()}

      for call in event.get_function_calls():
        used_any_tool = True
        if call.name in PLATFORM_TOOL_NAMES:
          yield {
            "type": "activity",
            "stage": "generating_test_cases",
            "label": "Generating test scenarios" if call.name == "propose_test_cases" else "Updating the test case",
            "toolName": call.name,
          }
        else:
          yield {"type": "activity", "stage": "analyzing", "label": describe_browser_tool(call.name), "toolName": call.name}
  except LlmCallsLimitExceededError:
    logger.warning("chat: turn %s hit the %d model-call cap", request.turnId, max_llm_calls)
  except Exception as err:  # noqa: BLE001
    logger.exception("chat: turn %s failed", request.turnId)
    yield {"type": "error", "code": "agent_error", "message": f"Agent error: {err}"}
    return

  yield {
    "type": "done",
    "text": assistant_text.strip(),
    "usedAnyTool": used_any_tool,
    "producedTestCases": produced_test_cases,
  }


# ---------------------------------------------------------------------------
# One-shot structured agents
# ---------------------------------------------------------------------------


async def run_structured(agent: LlmAgent, prompt: str, output_model: type[ModelT]) -> tuple[Optional[ModelT], str, Usage]:
  """Runs a single-turn agent and parses its JSON answer. Returns
  (parsed or None, raw final text, usage)."""
  sessions = InMemorySessionService()
  session = await sessions.create_session(app_name=APP_NAME, user_id="system")
  runner = Runner(agent=agent, app_name=APP_NAME, session_service=sessions)
  usage = Usage()
  final_text = ""
  async for event in runner.run_async(
    user_id="system",
    session_id=session.id,
    new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
    run_config=RunConfig(max_llm_calls=3),
  ):
    if event.author == "user" or event.partial:
      continue
    usage.add(event.usage_metadata)
    text = _text_of(event)
    if text:
      final_text = text
  return parse_json_model(final_text, output_model), final_text, usage


def parse_json_model(text: str, output_model: type[ModelT]) -> Optional[ModelT]:
  candidates = [text]
  start, end = text.find("{"), text.rfind("}")
  if 0 <= start < end:
    candidates.append(text[start : end + 1])
  for candidate in candidates:
    try:
      return output_model.model_validate(json.loads(candidate))
    except (ValueError, ValidationError):
      continue
  return None
