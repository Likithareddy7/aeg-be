"""Tools that save work into the AegisQA platform.

The tools only describe themselves to the model and hand the model's
arguments to a `PlatformClient`; the real work (validate, save, notify the
UI) lives in aegisqa/chat.py. Keeping that behind a small interface lets the
ADK dev UI (adk_apps/) and the tests swap in a dry-run client.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from .gemini_schema import to_genai_schema
from .schemas import PROPOSE_TEST_CASES_PARAMS, UPDATE_TEST_CASE_PARAMS

logger = logging.getLogger(__name__)

# Session-state keys the chat turn sets so tools know whose turn this is.
STATE_PROJECT_ID = "project_id"
STATE_USER_ID = "user_id"
STATE_URL_ID = "url_id"
STATE_USER_TEXT = "user_text"
STATE_TARGET_URL = "target_url"


class PlatformClient(Protocol):
  async def call_tool(self, name: str, context: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]: ...


class PlatformTool(BaseTool):
  def __init__(self, *, name: str, description: str, parameters: dict[str, Any], client: PlatformClient):
    super().__init__(name=name, description=description)
    self._parameters = parameters
    self._client = client

  def _get_declaration(self) -> types.FunctionDeclaration:
    return types.FunctionDeclaration(
      name=self.name,
      description=self.description,
      parameters=to_genai_schema(self._parameters),
    )

  async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> dict[str, Any]:
    state = tool_context.state
    context = {
      "projectId": state.get(STATE_PROJECT_ID),
      "userId": state.get(STATE_USER_ID),
      "urlId": state.get(STATE_URL_ID),
      "userText": state.get(STATE_USER_TEXT, ""),
    }
    try:
      result = await self._client.call_tool(self.name, context, args)
    except Exception as err:  # noqa: BLE001 -- surfaced to the model as a tool error
      logger.exception("platform tool %s failed", self.name)
      return {"content": f"Failed to apply {self.name}: {err}", "isError": True}
    return {"content": str(result.get("text", "")), "isError": bool(result.get("isError"))}


def build_platform_tools(client: PlatformClient) -> list[PlatformTool]:
  return [
    PlatformTool(
      name="propose_test_cases",
      description=(
        "Save one or more generated test cases so they appear in the UI for the "
        "user to review, edit, and run. Call this once you have inspected the "
        "real page (via the browser tools) and are ready to hand off concrete, "
        "resilient-selector test cases -- do not call it with guessed selectors "
        "you have not verified against a page snapshot."
      ),
      parameters=PROPOSE_TEST_CASES_PARAMS,
      client=client,
    ),
    PlatformTool(
      name="update_test_case",
      description=(
        "Apply an edit to an existing, already-saved test case in response to a "
        "user's chat instruction (e.g. 'change the password test data to ...', "
        "'add a step that checks the error message'). Only include the fields "
        "that changed; `steps`, if included, fully replaces the step list."
      ),
      parameters=UPDATE_TEST_CASE_PARAMS,
      client=client,
    ),
  ]


PLATFORM_TOOL_NAMES = {"propose_test_cases", "update_test_case"}
