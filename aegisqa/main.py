"""The AegisQA backend app: REST API, the /ws WebSocket, static artifacts.

Run with `python -m aegisqa.main` (or `uvicorn aegisqa.main:app`).
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional, Union

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from google.adk.models.base_llm import BaseLlm

from . import chat, services
from .agents.browser import BrowserSessionManager
from .api import auth, flows, projects, runs, test_cases
from .auth.sessions import SESSION_COOKIE_NAME, effective_project_role, user_for_session
from .config import Settings
from .storage import AREAS, ArtifactStore, build_artifact_store, content_type_for
from .tasks import Publisher, PubSubPublisher, Verifier, google_oidc_verifier, internal_router
from .memory import KINDS, build_memory_service
from .memory.embeddings import Embedder
from .memory.learning import backfill
from .config import settings as default_settings
from .db import models as m
from .db.base import dispose_engine, session_scope, utcnow
from .errors import ApiError, api_error_handler
from .execution import executor
from .ws import registry

logger = logging.getLogger("aegisqa")


def create_app(
  *,
  settings: Settings = default_settings,
  model: Union[str, BaseLlm, None] = None,
  browsers: Optional[BrowserSessionManager] = None,
  embedder: Optional[Embedder] = None,
  artifacts: Optional[ArtifactStore] = None,
  publisher: Optional[Publisher] = None,
  internal_verifier: Optional[Verifier] = None,
) -> FastAPI:
  """The model, browsers, embedder, artifact store, Pub/Sub publisher and
  internal-token verifier are injectable so tests run without GCP."""
  if settings.task_queue not in ("inline", "pubsub"):
    raise RuntimeError(f"Unknown TASK_QUEUE {settings.task_queue!r} (use inline or pubsub)")
  if publisher is None and settings.task_queue == "pubsub":
    missing = [
      name
      for name, value in (
        ("PUBSUB_RUNS_TOPIC", settings.pubsub_runs_topic),
        ("INTERNAL_AUDIENCE", settings.internal_audience),
        ("INTERNAL_INVOKERS", ",".join(settings.internal_invokers)),
      )
      if not value
    ]
    if missing:
      raise RuntimeError(f"TASK_QUEUE=pubsub needs {', '.join(missing)}")
    publisher = PubSubPublisher()
  services.configure(
    services.Services(
      model=model if model is not None else settings.gemini_model,
      browsers=browsers or BrowserSessionManager(settings),
      memory=build_memory_service(settings, embedder),
      artifacts=artifacts or build_artifact_store(settings),
      publisher=publisher,
    )
  )
  if not settings.database_url:
    raise RuntimeError("DATABASE_URL is not set -- point it at your PostgreSQL database.")
  if not settings.credential_encryption_key:
    logger.warning("CREDENTIAL_ENCRYPTION_KEY is not set -- saving environment credentials and integration tokens will fail.")
  # Playwright MCP writes traces/videos here before they're stored; with
  # ARTIFACT_STORAGE=local these are also where files are served from.
  for directory in (settings.screenshots_dir, settings.artifacts_dir, settings.visual_regression_dir):
    directory.mkdir(parents=True, exist_ok=True)

  @asynccontextmanager
  async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await executor.recover_interrupted_runs()
    # Bring memory up to date with anything created before it existed.
    backfill_task = asyncio.create_task(_backfill_memory())
    logger.info(
      "backend ready (model: %s, memory: %s semantic / %s keyword, artifacts: %s, tasks: %s)",
      services.get().model_name,
      settings.memory_semantic_backend,
      settings.memory_keyword_backend,
      settings.artifact_storage,
      settings.task_queue,
    )
    yield
    backfill_task.cancel()
    await services.get().browsers.close_all()
    await dispose_engine()

  app = FastAPI(title="AegisQA", version="0.1.0", lifespan=lifespan)
  app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.frontend_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
  )
  app.add_exception_handler(ApiError, api_error_handler)

  @app.exception_handler(Exception)
  async def unhandled(_request: Any, exc: Exception) -> Any:
    from fastapi.responses import JSONResponse

    logger.exception("unhandled request error")
    return JSONResponse({"error": str(exc)}, status_code=500)

  @app.get("/api/health")
  async def health() -> dict[str, Any]:
    return {"ok": True, "model": services.get().model_name}

  for module in (auth, projects, test_cases, runs, flows):
    app.include_router(module.router)
  # Worker progress (only reachable with a valid Google-signed token from
  # INTERNAL_INVOKERS).
  app.include_router(internal_router(settings, internal_verifier or google_oidc_verifier(settings)))

  # Screenshots, traces/videos and visual-regression images -- from local disk
  # or Cloud Storage, at the same URLs either way.
  for area in AREAS:
    app.add_api_route(f"/{area}/{{path:path}}", _artifact_route(area), methods=["GET"], include_in_schema=False)

  app.add_api_websocket_route("/ws", websocket_endpoint)
  return app


def _artifact_route(area: str):  # type: ignore[no-untyped-def]
  async def serve(path: str) -> Response:
    store = services.get().artifacts
    assert store is not None
    try:
      local = store.local_path(area, path)
      if local is not None:
        return FileResponse(local, media_type=content_type_for(path))
      data = await store.get(area, path)
    except ValueError:
      data = None
    if data is None:
      return Response(status_code=404)
    return Response(data, media_type=content_type_for(path), headers={"Cache-Control": "private, max-age=3600"})

  return serve


async def _backfill_memory() -> None:
  try:
    count = await backfill(None, KINDS)
    if count:
      logger.info("memory: indexed %s existing item(s)", count)
  except Exception:  # noqa: BLE001 -- best effort; search still works on what's indexed
    logger.exception("memory: startup backfill failed")


# ---------------------------------------------------------------------------
# WebSocket: chat turns and live run progress
# ---------------------------------------------------------------------------


async def websocket_endpoint(websocket: WebSocket) -> None:
  await websocket.accept()
  async with session_scope() as db:
    user = await user_for_session(db, websocket.cookies.get(SESSION_COOKIE_NAME))
    if user is None:
      await websocket.close(code=4001, reason="Not authenticated")
      return
    project_id = websocket.query_params.get("projectId")
    if not project_id:
      await websocket.close(code=4000, reason="projectId query param is required")
      return
    if await effective_project_role(db, user, project_id) is None:
      await websocket.close(code=4003, reason="You are not a member of this project.")
      return

  connection = registry.Connection(websocket, user.id)
  registry.join(project_id, connection)
  tasks: set[asyncio.Task[None]] = set()
  try:
    while True:
      raw = await websocket.receive_text()
      task = asyncio.create_task(_handle_message(project_id, raw, user.id))
      tasks.add(task)
      task.add_done_callback(tasks.discard)
  except WebSocketDisconnect:
    pass
  finally:
    registry.leave(project_id, connection)
    await connection.close()


async def _handle_message(project_id: str, raw: str, user_id: str) -> None:
  private = registry.chat_emit(project_id, user_id)
  try:
    message = json.loads(raw)
    if not isinstance(message, dict):
      raise ValueError
  except ValueError:
    registry.send_to_user(project_id, user_id, {"type": "error", "message": "Received malformed message."})
    return

  try:
    kind = message.get("type")
    if kind == "chat.send":
      url = await chat.latest_target_url(project_id, message.get("urlId"))
      if url is None:
        private({"type": "error", "message": "Add a target URL to this project before chatting with the agent."})
        return
      async with session_scope() as db:
        row = await db.get(m.TargetUrl, url.id)
        if row is not None:
          row.last_inspected_at = utcnow()
      await chat.run_chat_turn_streaming(
        project_id, url.id, url.url, str(message.get("text", "")), user_id, private, message.get("environmentId")
      )
    elif kind == "chat.cancel":
      chat.cancel_chat_turn(str(message.get("turnId", "")))
    elif kind == "run.start":
      test_case_id = str(message.get("testCaseId"))
      async with session_scope() as db:
        tc = await db.get(m.TestCase, test_case_id)
      if tc is None or tc.project_id != project_id:
        private({"type": "error", "message": "Test case not found in this project."})
        return
      await executor.start_test_case(
        test_case_id,
        "user",
        registry.project_emit(project_id),
        executor.ExecuteOptions(
          environment_id=message.get("environmentId"),
          continue_from_chat_session=bool(message.get("continueFromChatSession")),
          user_id=user_id,
        ),
      )
    elif kind == "run.rerunFailed":
      run_id = str(message.get("testRunId"))
      async with session_scope() as db:
        run = await db.get(m.TestRun, run_id)
        tc = await db.get(m.TestCase, run.test_case_id) if run else None
      if tc is None or tc.project_id != project_id:
        private({"type": "error", "message": "Test run not found in this project."})
        return
      await executor.rerun(run_id, registry.project_emit(project_id), message.get("resumeFromStepOrder"))
    else:
      private({"type": "error", "message": "Unknown message type."})
  except Exception as err:  # noqa: BLE001
    logger.exception("websocket: failed to handle message")
    private({"type": "error", "message": str(err)})


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _app() -> FastAPI:
  return create_app()


def run() -> None:
  uvicorn.run("aegisqa.main:_app", factory=True, host=default_settings.host, port=default_settings.port, log_level="info")


if __name__ == "__main__":
  run()
