# AegisQA backend

The AegisQA API and agents: Python, FastAPI and [Google ADK](https://google.github.io/adk-docs/)
agents on Gemini (Vertex AI), with PostgreSQL. The web UI is the separate
`aegisqa-frontend` repo.

Users enter a URL and chat in plain English; the test generation agent inspects the real
page in a browser (Playwright MCP), writes structured test cases, runs them, and reports
steps, screenshots, logs, failures and suggested fixes.

## Architecture

```
aegisqa-frontend (Next.js)
   │  REST + WebSocket (/ws)
   ▼
this backend ── FastAPI ─────────────────────────────────────────────────────┐
   │   auth + roles, projects, test cases, runs, reports, exports            │
   │                                                                         │
   ├── Google ADK agents ──► Gemini on Vertex AI                             │
   │      test generation · self-healing · failure analysis · step builder   │
   │                                                                         │
   ├── Playwright MCP browsers (npx @playwright/mcp, or a remote MCP server) │
   │      chat exploration · test execution                                  │
   │                                                                         │
   ├── Memory (aegisqa/memory/) -- learning loop + hybrid search:            │
   │      exact: Postgres · semantic: pgvector | Vertex AI Vector Search     │
   │      keyword: Postgres full-text | Firestore BM25                       │
   │                                                                         │
   ├── Artifacts: local disk | Cloud Storage                                 │
   ├── Test runs: in-process | Pub/Sub + GKE worker                         │
   │                                                                         │
   └── PostgreSQL (Cloud SQL on GCP) via SQLAlchemy + Alembic ───────────────┘
```

Every GCP service is optional locally: with the defaults the backend needs only
PostgreSQL (with pgvector) and, for the AI features, Gemini credentials. On GCP each piece
is switched on by environment variables -- see [`docs/GCP_HANDOFF.md`](./docs/GCP_HANDOFF.md).

## Setup

Prerequisites: Python 3.10+, Node.js 20+ (for the browsers), PostgreSQL 15+ with
pgvector, and Vertex AI access (`gcloud auth application-default login`) or a Google AI
Studio API key for local use.

```bash
# PostgreSQL (macOS): brew install postgresql@17 pgvector && brew services start postgresql@17
createdb aegisqa

python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"      # Windows: .venv\Scripts\pip install -e ".[dev]"
                                       # (or: pip install -r requirements.txt)
npm install                            # Playwright MCP, for the browsers
npx @playwright/mcp install-browser chrome-for-testing

cp .env.example .env                   # then fill it in -- see below
.venv/bin/alembic upgrade head         # create the tables
.venv/bin/python -m aegisqa.seed       # create the admin login (--demo also adds a demo project)
```

In `.env` set at least `DATABASE_URL`, `GOOGLE_CLOUD_PROJECT` (or `GOOGLE_API_KEY`),
`ADMIN_USERNAME` / `ADMIN_PASSWORD` and `CREDENTIAL_ENCRYPTION_KEY`. Every setting is
described in [`.env.example`](./.env.example).

On Windows, use `.venv\Scripts\python` / `.venv\Scripts\alembic` in place of
`.venv/bin/...` throughout.

## Run

```bash
.venv/bin/python -m aegisqa.main       # http://localhost:4000
```

`GET http://localhost:4000/api/health` shows the backend is up and which model it uses.
Then start the frontend (its README) and sign in with the admin account.

To try a single agent in ADK's dev UI: `.venv/bin/adk web adk_apps`.

## Structure

```
aegisqa/
  main.py          app factory, WebSocket handler, artifact routes
  api/             REST routes
  auth/            passwords, sessions, role checks (where SSO plugs in)
  agents/          ADK agents, prompts, tool schemas, browser sessions (McpToolset)
  chat.py          chat turns, the save-test-case tools, one-shot agent calls
  memory/          memory layer: stores, hybrid search, learning loop, backfill
  storage.py       artifact storage (local disk or Cloud Storage)
  tasks.py         Pub/Sub publishing and the /internal endpoints
  worker.py        test-run worker for queued execution (GKE)
  execution/       test runner, selector resolution, visual diffs, exporter, reports
  integrations/    Azure DevOps (API only; no UI)
  db/              models, queries, serializers
  cli/run_tests.py CI runner
  seed.py          first admin login
migrations/        Alembic migrations
adk_apps/          entry points for `adk web`
tests/             unit, in-process app, real-browser and API contract tests
docs/GCP_HANDOFF.md  deployment notes for the GCP engineer
Dockerfile         the backend + worker image
package.json       local development only: Playwright MCP for the browsers
```

## Tests

```bash
.venv/bin/python -m pytest                                    # no Gemini calls needed
TEST_DATABASE_URL=postgresql://localhost/aegisqa_test .venv/bin/python -m pytest   # + database tests
```

- Tests use a scripted stand-in for Gemini, so they run without credentials.
  `-m "not browser"` skips the ones that start a real Chromium.
- Database tests drop and recreate the schema of `TEST_DATABASE_URL`, so they refuse any
  database whose name doesn't contain "test".
- `tests/test_schemas.py` checks the step actions match the frontend's API types. It looks
  for the frontend repo next to this one (`../aegisqa-frontend`), or at `FRONTEND_REPO`,
  and is skipped if it can't find it.
- `tests/contract/` is a black-box suite for a *running* backend (set `CONTRACT_BASE_URL`,
  `CONTRACT_DATABASE_URL`, `CONTRACT_ADMIN_USERNAME`, `CONTRACT_ADMIN_PASSWORD`). It proves
  the API matches what the frontend expects.

## Behaviour worth knowing

- **Roles**: a site-wide `admin` (always treated as project owner) or `member`; per project
  `owner` (members, credentials), `editor` (create/edit/run), `reviewer` (approve/reject --
  never your own test case) and `viewer` (read + run). Security-relevant actions go to an
  audit log.
- **Credentials**: environment credentials are AES-256-GCM encrypted at rest and never
  returned by the API. Steps reference them as `{{env.KEY}}`; they're substituted
  server-side just before the browser action, so they never reach chat or the model.
- **Self-healing**: a broken selector is re-located by the self-healing agent, but the fix
  is only a suggestion until an editor or owner approves it.
- **Cost tracking**: every model call records tokens and an estimated cost. The per-model
  rates in `aegisqa/db/records.py` are placeholders -- set your contracted Gemini prices.

## CI/CD

- **Export**: `GET /api/projects/{id}/export-ci` zips every approved test case as a
  standalone `@playwright/test` project.
- **CLI runner**: `aegisqa-run-tests` signs in, runs every approved test case, writes
  `junit-results.xml` and exits non-zero on failure.
- **Webhook**: set `WEBHOOK_URL` for a Slack-compatible post when a run doesn't pass.

## Known limitations

- **One backend instance** (or session affinity): WebSocket rooms and open chat browsers
  live in process memory. See `docs/GCP_HANDOFF.md`.
- **Login is username/password** for now; the client's SSO plugs in at
  `aegisqa/auth/sessions.py`.
- **"Continue from chat session" needs local browsers** (not `PLAYWRIGHT_MCP_URL`).
- Not built yet from the target architecture: the knowledge graph, multi-instance
  WebSocket fan-out, SSO.
