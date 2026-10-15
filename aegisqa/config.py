"""Every setting, read once from the environment (or .env).

Each GCP connection -- the Cloud SQL URL, Vertex AI project/location, a
remote Playwright MCP endpoint -- is a plain env var, so deploying to GCP is a
configuration change. See docs/GCP_HANDOFF.md for which ones the GCP engineer
sets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
  raw = os.environ.get(name)
  if raw is None or raw.strip() == "":
    return default
  return raw.strip().lower() not in ("0", "false", "no", "off")


def _path(name: str, default: str) -> Path:
  return Path(os.environ.get(name) or default).resolve()


def _origins(raw: str | None) -> list[str]:
  defaults = ["http://localhost:3000", "http://127.0.0.1:3000"]
  configured = [value.strip() for value in (raw or "").split(",") if value.strip()]
  return list(dict.fromkeys(defaults + configured))


def _async_database_url(raw: str) -> str:
  """Accepts the usual `postgresql://` form (what Cloud SQL docs, psql and
  Prisma use) and selects SQLAlchemy's asyncpg driver."""
  for prefix in ("postgresql://", "postgres://"):
    if raw.startswith(prefix):
      return "postgresql+asyncpg://" + raw[len(prefix) :]
  return raw


@dataclass(frozen=True)
class Settings:
  # --- HTTP server ---
  host: str = field(default_factory=lambda: os.environ.get("HOST", "0.0.0.0"))
  port: int = field(default_factory=lambda: int(os.environ.get("PORT", "4000")))
  frontend_origins: list[str] = field(default_factory=lambda: _origins(os.environ.get("FRONTEND_ORIGIN")))

  # --- Database (Cloud SQL for PostgreSQL on GCP) ---
  database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", ""))

  # --- Model. ADK's Gemini client reads GOOGLE_GENAI_USE_VERTEXAI /
  # GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION (or GOOGLE_API_KEY) itself.
  gemini_model: str = field(default_factory=lambda: os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite"))
  max_llm_calls_per_turn: int = field(default_factory=lambda: int(os.environ.get("MAX_LLM_CALLS_PER_TURN", "20")))

  # --- Browsers (Playwright MCP). Unset PLAYWRIGHT_MCP_URL -> spawn
  # `npx @playwright/mcp` per session; set -> connect to a remote server.
  playwright_mcp_url: str = field(default_factory=lambda: os.environ.get("PLAYWRIGHT_MCP_URL", ""))
  playwright_browser: str = field(default_factory=lambda: os.environ.get("PLAYWRIGHT_BROWSER", "chromium"))
  playwright_headless: bool = field(default_factory=lambda: _bool("PLAYWRIGHT_HEADLESS", True))
  browser_idle_timeout_seconds: float = field(
    default_factory=lambda: float(os.environ.get("BROWSER_IDLE_TIMEOUT_SECONDS", str(10 * 60)))
  )
  # Per MCP tool call -- launching a cold browser on the first call is slow.
  browser_tool_timeout_seconds: float = field(
    default_factory=lambda: float(os.environ.get("BROWSER_TOOL_TIMEOUT_SECONDS", "120"))
  )

  # --- Files (screenshots, traces/videos, visual baselines) ---
  # "local" (the three directories below) or "gcs" (ARTIFACTS_BUCKET).
  artifact_storage: str = field(default_factory=lambda: os.environ.get("ARTIFACT_STORAGE", "local").lower())
  artifacts_bucket: str = field(default_factory=lambda: os.environ.get("ARTIFACTS_BUCKET", ""))
  screenshots_dir: Path = field(default_factory=lambda: _path("SCREENSHOTS_DIR", "./data/screenshots"))
  artifacts_dir: Path = field(default_factory=lambda: _path("ARTIFACTS_DIR", "./data/mcp-artifacts"))
  visual_regression_dir: Path = field(default_factory=lambda: _path("VISUAL_REGRESSION_DIR", "./data/visual-regression"))
  visual_diff_threshold_percent: float = field(
    default_factory=lambda: float(os.environ.get("VISUAL_DIFF_THRESHOLD_PERCENT", "0.1"))
  )

  # --- Memory layer (aegisqa/memory/) ---
  # Semantic search: "pgvector" (Cloud SQL / local Postgres, default),
  # "vertex_vector_search", or "none".
  memory_semantic_backend: str = field(default_factory=lambda: os.environ.get("MEMORY_SEMANTIC_BACKEND", "pgvector").lower())
  # Keyword search: "postgres" (full-text, default) or "firestore" (BM25).
  memory_keyword_backend: str = field(default_factory=lambda: os.environ.get("MEMORY_KEYWORD_BACKEND", "postgres").lower())
  # Vertex AI embedding model; vectors are 768-dimensional (fixed by the schema).
  embedding_model: str = field(default_factory=lambda: os.environ.get("EMBEDDING_MODEL", "gemini-embedding-001"))
  # Defaults to GOOGLE_CLOUD_LOCATION; set if the embedding model lives elsewhere.
  embedding_location: str = field(default_factory=lambda: os.environ.get("EMBEDDING_LOCATION", ""))
  # Vertex AI Vector Search (only with MEMORY_SEMANTIC_BACKEND=vertex_vector_search):
  # full resource names, e.g. projects/P/locations/us-central1/indexes/123.
  vector_search_index: str = field(default_factory=lambda: os.environ.get("VECTOR_SEARCH_INDEX", ""))
  vector_search_index_endpoint: str = field(default_factory=lambda: os.environ.get("VECTOR_SEARCH_INDEX_ENDPOINT", ""))
  vector_search_deployed_index_id: str = field(default_factory=lambda: os.environ.get("VECTOR_SEARCH_DEPLOYED_INDEX_ID", ""))
  # The index endpoint's domain (public endpoint) or private address, e.g.
  # 1234567.us-central1-123456789.vdb.vertexai.goog
  vector_search_endpoint_domain: str = field(default_factory=lambda: os.environ.get("VECTOR_SEARCH_ENDPOINT_DOMAIN", ""))
  # Firestore (only with MEMORY_KEYWORD_BACKEND=firestore).
  firestore_database: str = field(default_factory=lambda: os.environ.get("FIRESTORE_DATABASE", "(default)"))
  firestore_collection_prefix: str = field(default_factory=lambda: os.environ.get("FIRESTORE_COLLECTION_PREFIX", "aegisqa"))
  google_cloud_project: str = field(default_factory=lambda: os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
  google_cloud_location: str = field(default_factory=lambda: os.environ.get("GOOGLE_CLOUD_LOCATION", "global"))

  # --- Task queue (Pub/Sub) ---
  # "inline": test runs execute inside this process (local, default).
  # "pubsub": test runs go through PUBSUB_RUNS_TOPIC, consumed by
  # `python -m aegisqa.worker` (GKE) from PUBSUB_RUNS_SUBSCRIPTION.
  task_queue: str = field(default_factory=lambda: os.environ.get("TASK_QUEUE", "inline").lower())
  pubsub_runs_topic: str = field(default_factory=lambda: os.environ.get("PUBSUB_RUNS_TOPIC", ""))
  pubsub_runs_subscription: str = field(default_factory=lambda: os.environ.get("PUBSUB_RUNS_SUBSCRIPTION", ""))
  # Service-to-service calls into /internal/* carry a Google-signed OIDC token
  # whose audience is INTERNAL_AUDIENCE (the backend's URL) and whose email is
  # one of INTERNAL_INVOKERS (the worker's service account).
  internal_audience: str = field(default_factory=lambda: os.environ.get("INTERNAL_AUDIENCE", ""))
  internal_invokers: list[str] = field(
    default_factory=lambda: [e.strip() for e in os.environ.get("INTERNAL_INVOKERS", "").split(",") if e.strip()]
  )
  # Where the worker reaches the backend (for live run progress).
  backend_internal_url: str = field(default_factory=lambda: os.environ.get("BACKEND_INTERNAL_URL", "").rstrip("/"))

  # --- Misc ---
  webhook_url: str = field(default_factory=lambda: os.environ.get("WEBHOOK_URL", ""))
  credential_encryption_key: str = field(default_factory=lambda: os.environ.get("CREDENTIAL_ENCRYPTION_KEY", ""))
  admin_username: str = field(default_factory=lambda: os.environ.get("ADMIN_USERNAME") or "admin")
  admin_password: str = field(default_factory=lambda: os.environ.get("ADMIN_PASSWORD", ""))
  max_parallel_runs: int = field(default_factory=lambda: int(os.environ.get("MAX_PARALLEL_RUNS", "3")))

  @property
  def async_database_url(self) -> str:
    return _async_database_url(self.database_url)


settings = Settings()
