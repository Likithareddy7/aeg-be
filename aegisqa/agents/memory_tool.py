"""`search_project_memory`: lets the test generation agent check what the
platform already knows before writing tests -- existing test cases, past
failures, elements whose locators changed and filed defects."""

from __future__ import annotations

from typing import Any

from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_context import ToolContext

from .platform_tools import STATE_PROJECT_ID

MEMORY_TOOL_NAME = "search_project_memory"


async def search_project_memory(query: str, tool_context: ToolContext) -> dict[str, Any]:
  """Search this project's memory: existing test cases, past test failures (with
  error codes and suggested fixes), approved self-heals (elements whose locators
  changed) and filed defects. Use it before writing new test
  cases so you don't duplicate existing ones, and to learn about known problems
  on the page.

  Args:
    query: What to look for, e.g. "login with wrong password" or "checkout button".
  """
  from .. import services

  project_id = tool_context.state.get(STATE_PROJECT_ID)
  memory = services.get().memory
  if not project_id or memory is None:
    return {"results": [], "note": "Memory isn't available."}
  hits = await memory.search(project_id, query, limit=8)
  return {
    "results": [
      {
        "kind": h.kind,
        "title": h.title,
        "id": h.ref_id,
        "module": h.module,
        "errorCode": h.error_code,
        "status": h.metadata.get("status"),
        "details": h.content[:600],
      }
      for h in hits
    ]
  }


memory_tool = FunctionTool(search_project_memory)
