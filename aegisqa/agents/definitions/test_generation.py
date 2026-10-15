"""Test generation agent: explores the live app through Playwright MCP and
saves structured test cases through the platform tools."""

from __future__ import annotations

from typing import Any, Optional, Union

from google.adk.agents import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.models.base_llm import BaseLlm
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from ..browser import mcp_result_for_model
from ..memory_tool import MEMORY_TOOL_NAME, memory_tool
from ..platform_tools import PLATFORM_TOOL_NAMES, STATE_TARGET_URL
from ..prompts import test_generation_instruction


def _instruction(ctx: ReadonlyContext) -> str:
  # A callable instruction is passed to the model as-is (no {placeholder}
  # templating), which matters because the prompt contains literal JSON.
  return test_generation_instruction(ctx.state.get(STATE_TARGET_URL, "(no target URL set)"))


def _browser_results_as_text(
  tool: BaseTool, args: dict[str, Any], tool_context: ToolContext, tool_response: dict[str, Any]
) -> Optional[dict[str, Any]]:
  if tool.name in PLATFORM_TOOL_NAMES or tool.name == MEMORY_TOOL_NAME:
    return None
  return mcp_result_for_model(tool_response)


def build_test_generation_agent(
  model: Union[str, BaseLlm],
  browser: BaseToolset,
  platform_tools: list[BaseTool],
) -> LlmAgent:
  return LlmAgent(
    name="test_generation_agent",
    description="Explores the target app in a real browser and generates resilient, structured test cases.",
    model=model,
    instruction=_instruction,
    tools=[browser, *platform_tools, memory_tool],
    after_tool_callback=_browser_results_as_text,
    generate_content_config=types.GenerateContentConfig(max_output_tokens=8192),
  )
