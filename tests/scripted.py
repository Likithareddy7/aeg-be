"""A scripted model for app-level tests: answers the failure-analysis and
self-healing agents by their instructions, and plays `script` for chat."""

from __future__ import annotations

import json
from typing import Any

from .fakes import ScriptedLlm, text_response


def _step(order: int, action: str, description: str, value: str | None = None, button: str | None = None) -> dict[str, Any]:
  step: dict[str, Any] = {"order": order, "action": action, "description": description}
  if value is not None:
    step["value"] = value
  if button:
    step["selector"] = {"strategy": "role", "role": "button", "name": button, "description": f"{button} button"}
  return step


def make_test_case(title: str, button: str) -> dict[str, Any]:
  return {
    "title": title,
    "objective": title,
    "preconditions": "Logged out",
    "testData": {},
    "expectedResult": "As specified",
    "priority": "high",
    "type": "functional",
    "module": "Authentication",
    "steps": [_step(1, "navigate", "Open login", value="https://app.test/login"), _step(2, "click", f"Click {button}", button=button)],
  }


class AppModel(ScriptedLlm):
  async def generate_content_async(self, llm_request: Any, stream: bool = False):  # type: ignore[override]
    self._requests.append(llm_request)
    system = llm_request.config.system_instruction or ""
    if "self-healing test runner" in system:
      yield text_response(json.dumps({"ref": None, "confidence": "low", "note": "No such button", "matchedRole": None, "matchedName": None}))
    elif "failed Playwright test steps" in system or "analyse failed Playwright" in system:
      yield text_response(json.dumps({"explanation": "The button doesn't exist.", "suggestedFix": "Remove the step."}))
    else:
      yield self.script.pop(0)
