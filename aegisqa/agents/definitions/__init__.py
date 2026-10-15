"""ADK agent definitions, one module per agent (each `build_*` returns an LlmAgent).

Naming follows the AegisQA architecture:
  test_generation  -- chat-driven requirement understanding + live page
                      exploration + test case generation (Test Gen agent)
  self_healing     -- re-locates an element whose selector broke (Self-heal)
  failure_analysis -- explains a failed step and suggests a fix (Report)
  step_builder     -- turns one instruction into one low-code step (Script Gen)
"""

from .failure_analysis import build_failure_analysis_agent
from .self_healing import build_self_healing_agent
from .step_builder import build_step_builder_agent
from .test_generation import build_test_generation_agent

__all__ = [
  "build_failure_analysis_agent",
  "build_self_healing_agent",
  "build_step_builder_agent",
  "build_test_generation_agent",
]
