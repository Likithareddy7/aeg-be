from __future__ import annotations

from typing import Union

from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.genai import types

from ..prompts import STEP_BUILDER_INSTRUCTION
from ..schemas import GeneratedStep


def build_step_builder_agent(model: Union[str, BaseLlm]) -> LlmAgent:
  return LlmAgent(
    name="step_builder_agent",
    description="Turns one plain-English instruction into one structured, page-grounded test step.",
    model=model,
    instruction=STEP_BUILDER_INSTRUCTION,
    output_schema=GeneratedStep,
    generate_content_config=types.GenerateContentConfig(max_output_tokens=1024),
  )
