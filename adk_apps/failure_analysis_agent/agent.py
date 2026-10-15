"""`adk web adk_apps` entry point. Paste the same prompt the service builds
(see aegisqa/agents/prompts.py) to try this agent on its own."""

from aegisqa.agents.definitions import build_failure_analysis_agent
from aegisqa.config import settings

root_agent = build_failure_analysis_agent(settings.gemini_model)
