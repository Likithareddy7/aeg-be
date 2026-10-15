from __future__ import annotations

from typing import Union

from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.genai import types

from ..prompts import FAILURE_ANALYSIS_INSTRUCTION
from ..schemas import FailureExplanation


def build_failure_analysis_agent(model: Union[str, BaseLlm]) -> LlmAgent:
  return LlmAgent(
    name="failure_analysis_agent",
    description="Explains why a test step failed and suggests a fix.",
    model=model,
    instruction=FAILURE_ANALYSIS_INSTRUCTION,
    output_schema=FailureExplanation,
    generate_content_config=types.GenerateContentConfig(max_output_tokens=1024),
  )
