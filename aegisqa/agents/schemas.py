"""Tool-parameter schemas the model sees, and the structured outputs of the
one-shot agents.

The JSON Schemas are what the model is shown; the Pydantic `*In` models at
the bottom validate what it (or a REST client) actually sends. STEP_ACTIONS
must match TEST_STEP_ACTIONS in the frontend repo's shared/src/index.ts -- tests/test_schemas.py
checks that so the two can't silently drift.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

STEP_ACTIONS: list[str] = [
  "navigate",
  "click",
  "fill",
  "select",
  "check",
  "uncheck",
  "hover",
  "press",
  "waitFor",
  "assertVisible",
  "assertText",
  "assertUrl",
  "screenshot",
  "assertEnabled",
  "assertDisabled",
  "assertFormValid",
  "assertTableContains",
  "assertApiResponse",
  "assertAccessible",
  "assertVisualMatch",
]

SELECTOR_STRATEGIES = ["role", "label", "text", "placeholder", "altText", "testId", "css", "xpath"]
TEST_CASE_TYPES = ["smoke", "regression", "functional", "negative", "ui", "accessibility", "edgeCase"]
PRIORITIES = ["low", "medium", "high", "critical"]

# ---------------------------------------------------------------------------
# JSON Schemas for the platform tools (propose_test_cases / update_test_case).
# Ported 1:1 from the original TypeScript backend.
# ---------------------------------------------------------------------------

SELECTOR_JSON_SCHEMA: dict[str, Any] = {
  "type": "object",
  "description": (
    "Resilient element descriptor. Prefer role+name, then label, then "
    "placeholder, then visible text, then a data-testid. Only fall back to "
    "`css` if the element genuinely has no accessible role/name/label/text. "
    "Omit entirely for actions that don't target an element (navigate, "
    "waitFor, assertUrl, screenshot)."
  ),
  "properties": {
    "strategy": {"type": "string", "enum": SELECTOR_STRATEGIES},
    "role": {"type": "string", "description": "ARIA role, e.g. 'button', 'textbox', 'link'"},
    "name": {"type": "string", "description": "Accessible name for the role strategy"},
    "label": {"type": "string"},
    "placeholder": {"type": "string"},
    "altText": {"type": "string", "description": "Image/icon alt text"},
    "testId": {"type": "string"},
    "css": {"type": "string", "description": "Last-resort CSS selector"},
    "xpath": {"type": "string", "description": "Last-resort XPath, tried after css"},
    "description": {
      "type": "string",
      "description": (
        "Short human-readable description of the element, e.g. 'Email input field'. "
        "Always required -- used to re-locate the element if the exact selector "
        "no longer matches at execution time."
      ),
    },
  },
  "required": ["strategy", "description"],
}

STEP_JSON_SCHEMA: dict[str, Any] = {
  "type": "object",
  "properties": {
    "order": {"type": "integer", "minimum": 1},
    "action": {"type": "string", "enum": STEP_ACTIONS},
    "selector": SELECTOR_JSON_SCHEMA,
    "value": {
      "type": "string",
      "description": (
        "Text to type/select, key to press, URL to navigate to, or expected "
        "text/URL for assertions. Omit when not applicable."
      ),
    },
    "description": {
      "type": "string",
      "description": "Human-readable description of this step, shown in the UI and exported script comments.",
    },
  },
  "required": ["order", "action", "description"],
}

TEST_CASE_JSON_SCHEMA: dict[str, Any] = {
  "type": "object",
  "properties": {
    "title": {"type": "string"},
    "objective": {"type": "string", "description": "One-sentence goal of this test case."},
    "preconditions": {"type": "string", "description": "State required before running, e.g. 'User is logged out'."},
    "testData": {
      "type": "object",
      "description": "Key/value test data used by the steps, e.g. { username: '...', password: '...' }.",
      "additionalProperties": True,
    },
    "expectedResult": {"type": "string"},
    "priority": {"type": "string", "enum": PRIORITIES},
    "type": {"type": "string", "enum": TEST_CASE_TYPES},
    "module": {
      "type": "string",
      "description": (
        "Short functional-area tag for this test case, e.g. 'Authentication', 'Checkout', "
        "'Search'. Infer it from the page/feature being tested. Omit if genuinely unclear."
      ),
    },
    "steps": {"type": "array", "items": STEP_JSON_SCHEMA},
  },
  "required": ["title", "objective", "preconditions", "testData", "expectedResult", "priority", "type", "steps"],
}

PROPOSE_TEST_CASES_PARAMS: dict[str, Any] = {
  "type": "object",
  "properties": {"testCases": {"type": "array", "items": TEST_CASE_JSON_SCHEMA, "minItems": 1}},
  "required": ["testCases"],
}

UPDATE_TEST_CASE_PARAMS: dict[str, Any] = {
  "type": "object",
  "properties": {
    "testCaseId": {"type": "string"},
    "updates": {
      "type": "object",
      "properties": {
        "title": {"type": "string"},
        "objective": {"type": "string"},
        "preconditions": {"type": "string"},
        "testData": {"type": "object", "additionalProperties": True},
        "expectedResult": {"type": "string"},
        "priority": {"type": "string", "enum": PRIORITIES},
        "type": {"type": "string", "enum": TEST_CASE_TYPES},
        "module": {"type": "string"},
        "steps": {"type": "array", "items": STEP_JSON_SCHEMA},
      },
    },
    "summary": {"type": "string", "description": "One-sentence human-readable summary of what changed, shown in chat."},
  },
  "required": ["testCaseId", "updates", "summary"],
}

# ---------------------------------------------------------------------------
# Structured outputs of the one-shot agents (LlmAgent.output_schema). Field
# names are camelCase on purpose: they are the JSON contract the frontend
# already consumes.
# ---------------------------------------------------------------------------

StepAction = Literal[tuple(STEP_ACTIONS)]  # type: ignore[valid-type]
SelectorStrategy = Literal[tuple(SELECTOR_STRATEGIES)]  # type: ignore[valid-type]


class FailureExplanation(BaseModel):
  explanation: str = Field(description="Plain-language explanation of why this step likely failed.")
  suggestedFix: str = Field(
    description="Concrete, actionable suggestion to fix the test step or the app (e.g. updated selector, timing fix)."
  )


class SelectorRecovery(BaseModel):
  ref: Optional[str] = Field(description="The [ref=...] id of the best-matching element, or null if none match.")
  confidence: Literal["high", "low"]
  note: str = Field(description="One sentence explaining the match or why none was found.")
  matchedRole: Optional[str] = Field(
    description="The ARIA role of the matched element (e.g. 'button'), or null if none matched."
  )
  matchedName: Optional[str] = Field(
    description="The accessible name/text of the matched element, or null if none matched."
  )


class GeneratedSelector(BaseModel):
  strategy: SelectorStrategy
  role: Optional[str] = None
  name: Optional[str] = None
  label: Optional[str] = None
  placeholder: Optional[str] = None
  altText: Optional[str] = None
  testId: Optional[str] = None
  css: Optional[str] = None
  xpath: Optional[str] = None
  description: str


class GeneratedStep(BaseModel):
  action: StepAction
  selector: Optional[GeneratedSelector] = None
  value: Optional[str] = None
  description: str


# ---------------------------------------------------------------------------
# Input validation for test cases and steps -- used by the platform tools
# (propose_test_cases / update_test_case) and the REST routes. Fields left out
# stay out of the stored JSON (null selector fields are dropped).
# ---------------------------------------------------------------------------

TestCaseType = Literal[tuple(TEST_CASE_TYPES)]  # type: ignore[valid-type]
Priority = Literal[tuple(PRIORITIES)]  # type: ignore[valid-type]


class SelectorIn(BaseModel):
  strategy: SelectorStrategy
  role: Optional[str] = None
  name: Optional[str] = None
  label: Optional[str] = None
  placeholder: Optional[str] = None
  altText: Optional[str] = None
  testId: Optional[str] = None
  css: Optional[str] = None
  xpath: Optional[str] = None
  description: str


class StepIn(BaseModel):
  order: int = Field(ge=1)
  action: StepAction
  selector: Optional[SelectorIn] = None
  locatorCandidates: Optional[list[SelectorIn]] = None
  value: Optional[str] = None
  description: str
  enabled: Optional[bool] = None
  # Round-trips a step's flow provenance through an editor save; the agent never sets it.
  sourceFlowVersionId: Optional[str] = None

  def to_dict(self) -> dict[str, Any]:
    out: dict[str, Any] = {"order": self.order, "action": self.action, "description": self.description}
    if self.selector is not None:
      out["selector"] = self.selector.model_dump(exclude_none=True)
    if self.locatorCandidates is not None:
      out["locatorCandidates"] = [c.model_dump(exclude_none=True) for c in self.locatorCandidates]
    for key in ("value", "enabled", "sourceFlowVersionId"):
      if getattr(self, key) is not None:
        out[key] = getattr(self, key)
    return out


class TestCaseIn(BaseModel):
  title: str
  objective: str
  preconditions: str
  testData: dict[str, Any]
  expectedResult: str
  priority: Priority
  type: TestCaseType
  module: Optional[str] = None
  steps: list[StepIn] = Field(min_length=1)

  def to_dict(self) -> dict[str, Any]:
    return {**self.model_dump(exclude={"steps"}), "steps": [st.to_dict() for st in self.steps]}


class ProposeTestCasesIn(BaseModel):
  testCases: list[TestCaseIn] = Field(min_length=1)


class TestCaseUpdatesIn(BaseModel):
  title: Optional[str] = None
  objective: Optional[str] = None
  preconditions: Optional[str] = None
  testData: Optional[dict[str, Any]] = None
  expectedResult: Optional[str] = None
  priority: Optional[Priority] = None
  type: Optional[TestCaseType] = None
  module: Optional[str] = None
  tags: Optional[list[str]] = None
  steps: Optional[list[StepIn]] = None

  def to_dict(self) -> dict[str, Any]:
    """Only the fields the caller actually sent (an explicit `module: null`
    clears the module; an omitted field is left alone)."""
    out = self.model_dump(exclude_unset=True, exclude={"steps"})
    for key in ("title", "objective", "preconditions", "testData", "expectedResult", "priority", "type", "tags"):
      if key in out and out[key] is None:
        del out[key]
    if self.steps is not None:
      out["steps"] = [st.to_dict() for st in self.steps]
    return out


class UpdateTestCaseIn(BaseModel):
  testCaseId: str
  updates: TestCaseUpdatesIn
  summary: str
