"""Test doubles: a scripted LLM (so tests run without Vertex AI credentials),
a fake browser toolset, and a recording platform client."""

from __future__ import annotations

from typing import Any, AsyncGenerator, Callable, Optional, Union

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.genai import types
from pydantic import PrivateAttr


def usage(prompt: int = 100, output: int = 20) -> types.GenerateContentResponseUsageMetadata:
  return types.GenerateContentResponseUsageMetadata(prompt_token_count=prompt, candidates_token_count=output)


def text_response(text: str) -> LlmResponse:
  return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]), usage_metadata=usage())


def call_response(name: str, args: dict[str, Any], text: Optional[str] = None) -> LlmResponse:
  parts = [types.Part(text=text)] if text else []
  parts.append(types.Part(function_call=types.FunctionCall(name=name, args=args)))
  return LlmResponse(content=types.Content(role="model", parts=parts), usage_metadata=usage())


Step = Union[LlmResponse, Callable[[LlmRequest], LlmResponse]]


class ScriptedLlm(BaseLlm):
  """Returns the scripted responses in order, one per model call. When
  streaming, a response's text is also emitted as a partial chunk first, the
  way Gemini's SSE stream does."""

  model: str = "scripted-test-model"
  script: list[Any] = []
  _requests: list[LlmRequest] = PrivateAttr(default_factory=list)

  @property
  def requests(self) -> list[LlmRequest]:
    return self._requests

  async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
    self._requests.append(llm_request)
    index = len(self._requests) - 1
    if index >= len(self.script):
      raise AssertionError(f"ScriptedLlm got unexpected call #{index + 1}")
    step = self.script[index]
    response = step(llm_request) if callable(step) else step
    text = "".join(p.text or "" for p in (response.content.parts if response.content else []) if p.text)
    if stream and text:
      yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]), partial=True)
    yield response


class FakeBrowserTool(BaseTool):
  def __init__(self, name: str, result: dict[str, Any], calls: list[tuple[str, dict[str, Any]]]):
    super().__init__(name=name, description=f"fake {name}")
    self._result = result
    self._calls = calls

  def _get_declaration(self) -> types.FunctionDeclaration:
    return types.FunctionDeclaration(
      name=self.name,
      description=self.description,
      parameters=types.Schema(type=types.Type.OBJECT, properties={"url": types.Schema(type=types.Type.STRING)}),
    )

  async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> dict[str, Any]:
    self._calls.append((self.name, args))
    return self._result


def png_base64(color: tuple[int, int, int], size: tuple[int, int] = (40, 30)) -> str:
  """A real PNG (solid colour) as base64, like browser_take_screenshot returns."""
  import base64
  import io

  from PIL import Image

  buffer = io.BytesIO()
  Image.new("RGB", size, color).save(buffer, format="PNG")
  return base64.b64encode(buffer.getvalue()).decode()


SNAPSHOT_TEXT = '- textbox "Email" [ref=e1]\n- button "Sign in" [ref=e2]'


class FakeBrowser(BaseToolset):
  """Answers the browser tools the agents and the test runner use; any other
  tool (tracing, console logs, ...) just succeeds."""

  def __init__(self) -> None:
    import tempfile
    from pathlib import Path

    super().__init__()
    self.calls: list[tuple[str, dict[str, Any]]] = []
    self.output_dir = Path(tempfile.mkdtemp(prefix="fake-browser-"))
    self._results = {
      "browser_navigate": {"content": [{"type": "text", "text": "Navigated to https://app.test/login"}], "isError": False},
      "browser_snapshot": {"content": [{"type": "text", "text": SNAPSHOT_TEXT}], "isError": False},
      "browser_take_screenshot": {"content": [{"type": "image", "data": png_base64((255, 0, 0)), "mimeType": "image/png"}]},
    }

  async def get_tools(self, readonly_context: Any = None) -> list[BaseTool]:
    return [FakeBrowserTool(name, result, self.calls) for name, result in self._results.items()]

  async def call_tool(self, name: str, args: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    self.calls.append((name, args or {}))
    return self._results.get(name, {"content": [{"type": "text", "text": "ok"}], "isError": False})

  async def close(self) -> None:
    pass


class FakeBrowsers:
  def __init__(self, fail: bool = False) -> None:
    self.browser = FakeBrowser()
    self.fail = fail
    self.opened: list[str] = []
    self.closed: list[str] = []

  def has_session(self, key: str) -> bool:
    return key in self.opened and key not in self.closed

  async def get(self, key: str, config: Any = None) -> FakeBrowser:
    if self.fail:
      raise RuntimeError("npx not found")
    self.opened.append(key)
    return self.browser

  async def close(self, key: str) -> None:
    self.closed.append(key)

  async def close_all(self) -> None:
    pass

  async def capture_state(self, key: str) -> tuple[dict[str, Any], str]:
    return {"cookies": [{"name": "sid"}]}, "https://app.test/home"


class RecordingPlatformClient:
  def __init__(self) -> None:
    self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

  async def call_tool(self, name: str, context: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    self.calls.append((name, context, args))
    return {"text": f"Saved {len(args.get('testCases', []))} test case(s).", "isError": False}


class FakeEmbedder:
  """Deterministic 768-d bag-of-words vectors: texts sharing words are close,
  so semantic search behaves sensibly without an embedding model."""

  async def embed(self, text: str, *, query: bool = False) -> Optional[list[float]]:
    import hashlib
    import math

    from aegisqa.memory.bm25 import tokenize

    vector = [0.0] * 768
    for word in tokenize(text):
      vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % 768] += 1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector] if any(vector) else None


class MemoryArtifactStore:
  """Artifact store kept in a dict -- stands in for Cloud Storage."""

  def __init__(self) -> None:
    self.objects: dict[str, bytes] = {}

  async def put(self, area: str, path: str, data: bytes) -> None:
    self.objects[f"{area}/{path}"] = data

  async def get(self, area: str, path: str) -> Optional[bytes]:
    return self.objects.get(f"{area}/{path}")

  async def put_file(self, area: str, path: str, local: Any) -> None:
    self.objects[f"{area}/{path}"] = local.read_bytes()

  def local_path(self, area: str, path: str) -> Any:
    return None

