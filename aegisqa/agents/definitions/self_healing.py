from __future__ import annotations

from typing import Union

from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.genai import types

from ..prompts import SELF_HEALING_INSTRUCTION
from ..schemas import SelectorRecovery


def build_self_healing_agent(model: Union[str, BaseLlm]) -> LlmAgent:
  return LlmAgent(
    name="self_healing_agent",
    description="Re-locates an element whose stored selector no longer matches the page.",
    model=model,
    instruction=SELF_HEALING_INSTRUCTION,
    output_schema=SelectorRecovery,
    generate_content_config=types.GenerateContentConfig(max_output_tokens=512),
  )
