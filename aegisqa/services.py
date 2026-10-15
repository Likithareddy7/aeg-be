"""Process-wide services, created once by the app factory: which model the
agents use (the Gemini model name, or a test double), the browser session
manager shared by chat exploration and test execution, the memory layer and
where screenshots / traces / videos are stored."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Union

from google.adk.models.base_llm import BaseLlm

from .agents.browser import BrowserSessionManager

if TYPE_CHECKING:
  from .memory import MemoryService
  from .storage import ArtifactStore
  from .tasks import Publisher


@dataclass
class Services:
  model: Union[str, BaseLlm]
  browsers: BrowserSessionManager
  memory: Optional["MemoryService"] = None
  artifacts: Optional["ArtifactStore"] = None
  # Set when TASK_QUEUE=pubsub: test runs are published instead of running in
  # this process.
  publisher: Optional["Publisher"] = None

  @property
  def model_name(self) -> str:
    return self.model if isinstance(self.model, str) else self.model.model


_services: Optional[Services] = None


def configure(services: Services) -> None:
  global _services
  _services = services


def get() -> Services:
  if _services is None:
    raise RuntimeError("Services are not configured -- create the app with aegisqa.main.create_app().")
  return _services
