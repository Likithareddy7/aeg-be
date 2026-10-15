"""Index existing data into memory (items that have no memory record yet).

  python -m aegisqa.memory.backfill [--project <id>]

Safe to re-run. Useful after switching memory backends or enabling embeddings.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from .. import services
from ..agents.browser import BrowserSessionManager
from ..config import settings
from ..db.base import dispose_engine
from . import KINDS, build_memory_service
from .learning import backfill


async def main(project_id: str | None) -> None:
  services.configure(
    services.Services(model=settings.gemini_model, browsers=BrowserSessionManager(settings), memory=build_memory_service(settings))
  )
  try:
    count = await backfill(project_id, KINDS)
    print(f"Indexed {count} item(s).")
  finally:
    await dispose_engine()


if __name__ == "__main__":
  logging.basicConfig(level=logging.INFO)
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--project")
  asyncio.run(main(parser.parse_args().project))
