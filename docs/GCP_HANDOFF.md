# AegisQA: GCP deployment handoff

For the GCP engineer deploying AegisQA. The application code is written and tested
locally, with fakes standing in for the GCP services. Networking, identity, IAM and
infrastructure are yours. Every connection point is an environment variable, so none of
it needs code changes. Anything that *does* still need code is listed under
[Not in the code yet](#not-in-the-code-yet).

## What gets deployed

| Workload | Source | Runs as | Talks to |
|---|---|---|---|
| **web** | the `aegisqa-frontend` repo (Next.js 14): its `Dockerfile`, port 3000 | Cloud Run | backend (REST + WebSocket `/ws`) |
| **backend** | this repo (Python, FastAPI, Google ADK): `Dockerfile`, port 4000 | Cloud Run | Cloud SQL, Vertex AI, Cloud Storage, Firestore, Vector Search, Pub/Sub, the app under test |
| **worker** | same image, command `python -m aegisqa.worker` | GKE Autopilot Deployment | Pub/Sub, Cloud SQL, Vertex AI, Cloud Storage, Firestore, Vector Search, the app under test, backend `/internal/events` |
| **jobs** | same image: `alembic upgrade head`, `python -m aegisqa.seed`, `python -m aegisqa.memory.backfill` | Cloud Run jobs | Cloud SQL (backfill also: Vertex AI, Firestore, Vector Search) |

Build the backend image from this repo's root: `docker build -t aegisqa-backend .`.
**It hasn't been built yet** (Docker wasn't available while writing it), so build it and
check `GET /api/health` first.

```
browser ──HTTPS / WSS──▶ web, backend (/api/*, /ws, /screenshots/*, /artifacts/*, /visual-regression/*)
backend ──publish──▶ Pub/Sub  aegisqa-runs ──pull──▶ worker ──POST (OIDC)──▶ backend /internal/events
backend, worker ──▶ Cloud SQL (PostgreSQL + pgvector) · Vertex AI (Gemini, embeddings, Vector Search)
                ──▶ Cloud Storage (artifacts) · Firestore (BM25 keyword index)
backend, worker ──▶ staging app under test (headless Chromium via Playwright MCP; never production)
```

## Resources to create

| Resource | Details |
|---|---|
| **Cloud SQL for PostgreSQL 15+** | Database `aegisqa`. The `vector` extension is created by a migration, so the migration user needs `cloudsqlsuperuser` (the default user has it). |
| **Vertex AI** | Enable the API. Used for Gemini (`GEMINI_MODEL`) and embeddings (`EMBEDDING_MODEL`, default `gemini-embedding-001`, 768 dimensions). |
| **Vector Search index** | Dimensions **768**, distance **COSINE_DISTANCE**, update method **STREAM_UPDATE**. Deploy it to an index endpoint (public, or Private Service Connect). Restrict namespaces used: `project`, `kind`. |
| **Firestore** (Native mode) | Composite index on collection **`aegisqa_bm25_docs`**: `projectId` ASC + `terms` ARRAY_CONTAINS. The prefix follows `FIRESTORE_COLLECTION_PREFIX`. Collections are created on first write. |
| **Cloud Storage bucket** | Private (no public access). Holds `screenshots/`, `artifacts/` (traces, videos) and `visual-regression/`. The backend streams files to users, so no signed URLs are needed. |
| **Pub/Sub** | One topic and one subscription, below. |

### Pub/Sub

| Topic | Subscription | Type | Settings |
|---|---|---|---|
| `aegisqa-runs` | `aegisqa-runs-worker` | **Pull**, by the worker | Ack deadline 600 s; the client extends leases while a run is going. A dead-letter topic is recommended. |

Redelivered messages are safe: a run that has already started is skipped.

## Service accounts and roles

| Account | Roles |
|---|---|
| **backend** (Cloud Run) | `roles/cloudsql.client`, `roles/aiplatform.user`, `roles/datastore.user`, `roles/pubsub.publisher`, `roles/storage.objectAdmin` on the bucket |
| **worker** (GKE, via Workload Identity) | `roles/cloudsql.client`, `roles/aiplatform.user`, `roles/datastore.user`, `roles/pubsub.subscriber` on `aegisqa-runs-worker`, `roles/storage.objectAdmin` on the bucket |

`INTERNAL_INVOKERS` is the **worker** account's email. The backend accepts `/internal/*`
calls only with a Google-signed ID token for `INTERNAL_AUDIENCE` from that account. Block `/internal/*` at the load balancer / Apigee too.

## Environment variables

All of these are documented in `.env.example`. Put secrets in Secret
Manager.

**Backend and worker:**

| Variable | Value |
|---|---|
| `DATABASE_URL` | Cloud SQL URL, e.g. `postgresql://USER:PASSWORD@/aegisqa?host=/cloudsql/PROJECT:REGION:INSTANCE` *(secret)* |
| `GOOGLE_GENAI_USE_VERTEXAI` / `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` | `TRUE` / project id / `global` (or a region that serves the models) |
| `GEMINI_MODEL`, `EMBEDDING_MODEL`, `EMBEDDING_LOCATION` | Defaults are fine. Set `EMBEDDING_LOCATION` only if embeddings aren't available in `GOOGLE_CLOUD_LOCATION`. |
| `CREDENTIAL_ENCRYPTION_KEY` | 64 hex chars *(secret; never change it after first use, or saved credentials become unreadable)* |
| `MEMORY_SEMANTIC_BACKEND` | `vertex_vector_search` (or keep `pgvector`; both work) |
| `VECTOR_SEARCH_INDEX`, `VECTOR_SEARCH_INDEX_ENDPOINT`, `VECTOR_SEARCH_DEPLOYED_INDEX_ID`, `VECTOR_SEARCH_ENDPOINT_DOMAIN` | Full resource names, plus the endpoint's domain (public) or address (private) |
| `MEMORY_KEYWORD_BACKEND` | `firestore` (or keep `postgres`; both work) |
| `FIRESTORE_DATABASE`, `FIRESTORE_COLLECTION_PREFIX` | `(default)` / `aegisqa` |
| `ARTIFACT_STORAGE`, `ARTIFACTS_BUCKET` | `gcs` / bucket name |
| `TASK_QUEUE` | `pubsub` |
| `PUBSUB_RUNS_TOPIC` | `projects/P/topics/aegisqa-runs` |
| `INTERNAL_AUDIENCE` | The backend's URL |
| `INTERNAL_INVOKERS` | The worker service account's email |
| `MAX_PARALLEL_RUNS` | Worker: runs at once per pod. |
| `PLAYWRIGHT_HEADLESS` | `true` |

**Backend only:** `FRONTEND_ORIGIN` (the web URL), `PORT=4000`, and
`ADMIN_USERNAME` / `ADMIN_PASSWORD` *(secret; read only by the seed job)*.

**Worker only:**
- `PUBSUB_RUNS_SUBSCRIPTION` = `projects/P/subscriptions/aegisqa-runs-worker`
- `BACKEND_INTERNAL_URL` = the backend's URL

**web:** `NEXT_PUBLIC_API_BASE_URL` (`https://<backend>`) and `NEXT_PUBLIC_WS_BASE_URL`
(`wss://<backend>`), passed as `--build-arg`s to the frontend's `docker build`: they're
baked in at build time. See the frontend repo's README.

## Each deploy

Run these as Cloud Run jobs from the image, with the backend's environment:

```bash
alembic upgrade head                 # schema; safe to re-run
python -m aegisqa.seed               # first admin login; no-op once users exist
python -m aegisqa.memory.backfill    # only after switching memory backends; safe to re-run
```

Never run `python -m aegisqa.seed --demo` against the client database: it adds a demo
project.

## Things that affect how you configure it

1. **Request durations.**
   - With `TASK_QUEUE=pubsub`, test-run requests return immediately.
   - Chat streams over the WebSocket for as long as the agent works. Allow WebSockets
     through the load balancer / Apigee, and don't buffer responses.
2. **One backend instance, or session affinity.** WebSocket rooms and each user's open
   chat browser live in the memory of the backend instance serving them. Run events from
   workers are forwarded to *the instance that receives them*. Start with
   `min-instances=1, max-instances=1`. Scaling the backend out needs a shared event
   fan-out (e.g. Memorystore Redis), which is code work.
3. **Browsers.**
   - The backend still runs headless Chromium for chat exploration and quick-add step:
     budget ~1 vCPU / 1 GB per concurrent chat, plus 1 GB `/dev/shm`.
   - Workers run the test browsers: ~1 vCPU / 1 GB per run, times `MAX_PARALLEL_RUNS`.
     Scale workers with replicas (or KEDA on subscription backlog).
4. **Egress.** Backend and worker browsers must reach the staging app under test, and
   only staging. They must never reach production.
5. **Remote browsers (optional).** Instead of local Chromium, set `PLAYWRIGHT_MCP_URL` to a
   Playwright MCP server (`npx @playwright/mcp --port 8931 --headless --isolated --caps devtools,storage`).
   In that mode, "continue from chat session" doesn't carry cookies over.
6. **Memory degrades gracefully.** If embeddings, Vector Search or Firestore are
   unreachable, memory keeps working on PostgreSQL alone and logs a warning. Look for
   `embeddings unavailable` / `Vector Search` / `Firestore` warnings when verifying.

## Not in the code yet

- **Login is username/password with server-side sessions** (a deliberate choice for now).
  The client's SSO plugs in at `aegisqa/auth/sessions.py`
  (`current_user` / `user_for_session`); every REST route and the WebSocket go through it.
- **The "knowledge graph" from the architecture diagram isn't built.** Run results and
  defects are stored as memory records (searchable, with error codes), not as a graph
  with risk scores.
- **Multi-instance WebSocket fan-out** (point 2 above).
- **Model Armor / Apigee policies** are GCP configuration, not code.

## Smoke test after deploying

1. `GET <backend>/api/health` returns `{"ok": true, "model": "<GEMINI_MODEL>"}`. The
   backend log line `backend ready (...)` shows which memory, artifact and task backends
   are active.
2. Sign in to the web app with the seeded admin.
3. Create a project with a staging URL and send "generate test cases for this page". It
   should go "Navigating the browser..." → "Generating test scenarios" → "Test cases ready".
   - "Couldn't start the browser session": Chromium / Playwright MCP isn't usable in the
     container.
   - "Agent error … permission": Vertex AI credentials or IAM.
4. `GET <backend>/api/projects/<id>/memory?q=login` (signed in) lists the new test cases,
   with `matchedBy` containing `semantic` once embeddings work.
5. Run a test case. The API returns a *pending* run, the worker picks it up, and the run
   page fills in live. Screenshots load from Cloud Storage. A failing step should get a
   suggested fix from Gemini.
