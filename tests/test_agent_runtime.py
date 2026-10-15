"""The ADK agent runtime with a scripted model (no Gemini credentials needed)."""

from __future__ import annotations

from typing import Any

from aegisqa.agents.definitions import build_failure_analysis_agent, build_self_healing_agent
from aegisqa.agents.runtime import ChatTurnRequest, run_chat_turn, run_structured
from aegisqa.agents.schemas import FailureExplanation, SelectorRecovery

from .fakes import FakeBrowsers, RecordingPlatformClient, ScriptedLlm, call_response, text_response

TEST_CASE = {
  "title": "Login rejects a wrong password",
  "objective": "Verify an error is shown for invalid credentials.",
  "preconditions": "User is logged out",
  "testData": {"email": "a@b.test", "password": "wrong"},
  "expectedResult": "An error message is shown",
  "priority": "high",
  "type": "negative",
  "module": "Authentication",
  "steps": [{"order": 1, "action": "navigate", "value": "https://app.test/login", "description": "Open login"}],
}


def request(**overrides: Any) -> ChatTurnRequest:
  body = {
    "turnId": "turn-1",
    "projectId": "proj-1",
    "userId": "user-1",
    "urlId": "url-1",
    "targetUrl": "https://app.test/login",
    "userText": "test invalid login",
    "history": [],
    "browserSessionKey": "chat:proj-1:user-1",
  }
  body.update(overrides)
  return ChatTurnRequest.model_validate(body)


async def collect(llm: ScriptedLlm, browsers: Any = None, platform: Any = None, max_calls: int = 20, **overrides: Any) -> list[dict[str, Any]]:
  return [
    event
    async for event in run_chat_turn(
      request(**overrides),
      model=llm,
      browsers=browsers or FakeBrowsers(),
      platform_client=platform or RecordingPlatformClient(),
      max_llm_calls=max_calls,
    )
  ]


async def test_chat_turn_explores_the_page_and_saves_test_cases() -> None:
  llm = ScriptedLlm(
    script=[
      call_response("browser_navigate", {"url": "https://app.test/login"}, text="Let me open the page."),
      call_response("browser_take_screenshot", {}),
      call_response("propose_test_cases", {"testCases": [TEST_CASE]}),
      text_response("Saved one negative login test."),
    ]
  )
  browsers, platform = FakeBrowsers(), RecordingPlatformClient()
  events = await collect(llm, browsers, platform)

  assert events[0] == {"type": "activity", "stage": "understanding", "label": "Understanding the test objective"}
  assert events[-1] == {
    "type": "done",
    "text": "Let me open the page.Saved one negative login test.",
    "usedAnyTool": True,
    "producedTestCases": True,
  }
  # Streamed text isn't repeated when the aggregated response arrives.
  assert [e["text"] for e in events if e["type"] == "delta"] == ["Let me open the page.", "Saved one negative login test."]
  assert [e for e in events if e["type"] == "usage"] == [
    {"type": "usage", "model": "scripted-test-model", "inputTokens": 100, "outputTokens": 20}
  ] * 4
  assert [(e["stage"], e.get("toolName")) for e in events if e["type"] == "activity"] == [
    ("understanding", None),
    ("analyzing", "browser_navigate"),
    ("analyzing", "browser_take_screenshot"),
    ("generating_test_cases", "propose_test_cases"),
  ]
  assert browsers.browser.calls[0] == ("browser_navigate", {"url": "https://app.test/login"})
  [(name, context, args)] = platform.calls
  assert name == "propose_test_cases" and args["testCases"][0]["title"] == TEST_CASE["title"]
  assert context == {"projectId": "proj-1", "userId": "user-1", "urlId": "url-1", "userText": "test invalid login"}

  # The model sees browser results as text only -- screenshot bytes are replaced.
  responses = [p.function_response.response for c in llm.requests[-1].contents for p in c.parts or [] if p.function_response]
  assert responses == [
    {"content": "Navigated to https://app.test/login", "isError": False},
    {"content": "(tool returned image content only)", "isError": False},
    {"content": "Saved 1 test case(s).", "isError": False},
  ]
  system = llm.requests[0].config.system_instruction
  assert "The current target application is: https://app.test/login" in system
  assert '{"urlPattern": "/api/login", "expectedStatus": 200}' in system  # literal JSON survives


async def test_chat_turn_replays_history_and_reports_no_tool_use() -> None:
  llm = ScriptedLlm(script=[text_response("Which checkout flow do you mean?")])
  history = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "Hi! What should I test?"}]
  events = await collect(llm, history=history, userText="test checkout")
  assert events[-1]["usedAnyTool"] is False
  assert [(c.role, c.parts[0].text) for c in llm.requests[0].contents] == [
    ("user", "hello"),
    ("model", "Hi! What should I test?"),
    ("user", "test checkout"),
  ]


async def test_chat_turn_reports_an_unavailable_browser() -> None:
  events = await collect(ScriptedLlm(), browsers=FakeBrowsers(fail=True))
  assert len(events) == 1 and events[0]["type"] == "error" and events[0]["code"] == "browser_unavailable"


async def test_chat_turn_stops_quietly_at_the_model_call_cap() -> None:
  llm = ScriptedLlm(script=[call_response("browser_snapshot", {})] * 30)
  events = await collect(llm, max_calls=3)
  assert len(llm.requests) == 3 and events[-1]["type"] == "done"


async def test_model_errors_become_an_error_event() -> None:
  def boom(_request: Any) -> Any:
    raise RuntimeError("quota exceeded")

  events = await collect(ScriptedLlm(script=[boom]))
  assert events[-1] == {"type": "error", "code": "agent_error", "message": "Agent error: quota exceeded"}


async def test_structured_agents_parse_json_and_fall_back() -> None:
  ok = ScriptedLlm(script=[text_response('{"explanation": "Renamed", "suggestedFix": "Use Log in"}')])
  parsed, _, usage = await run_structured(build_failure_analysis_agent(ok), "prompt", FailureExplanation)
  assert parsed == FailureExplanation(explanation="Renamed", suggestedFix="Use Log in")
  assert (usage.input_tokens, usage.output_tokens) == (100, 20)

  wrapped = ScriptedLlm(
    script=[text_response('Here you go: {"ref": "e2", "confidence": "high", "note": "n", "matchedRole": "button", "matchedName": "Go"}')]
  )
  parsed_recovery, _, _ = await run_structured(build_self_healing_agent(wrapped), "prompt", SelectorRecovery)
  assert parsed_recovery is not None and parsed_recovery.ref == "e2"

  garbage = ScriptedLlm(script=[text_response("no idea")])
  parsed_none, raw, _ = await run_structured(build_failure_analysis_agent(garbage), "prompt", FailureExplanation)
  assert parsed_none is None and raw == "no idea"
