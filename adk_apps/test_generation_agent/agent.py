"""`adk web adk_apps` entry point for the test generation agent.

Runs against a real browser (Playwright MCP) with Gemini, but saves test
cases with a dry-run platform client -- nothing is written to the AegisQA
database. Tell the agent which URL to test in your first message.
"""

from typing import Any

from aegisqa.agents.definitions import build_test_generation_agent
from aegisqa.agents.browser import BrowserConfig, BrowserSessionManager
from aegisqa.config import settings
from aegisqa.agents.platform_tools import build_platform_tools


class DryRunPlatformClient:
  async def call_tool(self, name: str, context: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    if name == "propose_test_cases":
      titles = [tc.get("title", "?") for tc in args.get("testCases", [])]
      return {"text": f"(dry run, not persisted) Saved {len(titles)} test case(s): {', '.join(titles)}.", "isError": False}
    return {"text": f"(dry run, not persisted) {name} accepted: {args.get('summary', '')}", "isError": False}


_browser = BrowserSessionManager(settings)._create("adk-web", BrowserConfig())

root_agent = build_test_generation_agent(
  settings.gemini_model,
  _browser,
  build_platform_tools(DryRunPlatformClient()),
)
