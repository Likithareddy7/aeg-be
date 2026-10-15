# AegisQA backend image: Python API + agents, plus Node and Chromium for the
# Playwright MCP browsers it starts for chat exploration and test runs.
#
# Build from this repo's root:
#   docker build -t aegisqa-backend .
#
# NOTE: not yet built or run in CI -- the GCP engineer should build it and
# smoke test GET /api/health before the first deploy.
FROM node:22-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=4000 \
    PLAYWRIGHT_HEADLESS=true \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv python3-pip ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Playwright MCP (in /app/node_modules, so `npx @playwright/mcp` run from
# /app/backend finds it) + the matching Chromium build and its OS libraries.
WORKDIR /app
RUN npm init -y > /dev/null \
 && npm install --omit=dev @playwright/mcp@0.0.78 \
 && npx playwright install-deps chromium \
 && npx @playwright/mcp install-browser chrome-for-testing \
 && chmod -R a+rX /ms-playwright

# The backend.
COPY pyproject.toml alembic.ini ./backend/
COPY aegisqa ./backend/aegisqa
COPY migrations ./backend/migrations
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir ./backend
ENV PATH="/opt/venv/bin:$PATH"

RUN useradd --create-home --uid 10001 app && mkdir -p /app/backend/data && chown -R app /app/backend/data
USER app
WORKDIR /app/backend

EXPOSE 4000
# The same image runs the test-run worker on GKE: command `python -m aegisqa.worker`.
# Migrations + first admin are one-off jobs (see docs/GCP_HANDOFF.md):
#   alembic upgrade head && python -m aegisqa.seed
CMD ["sh", "-c", "uvicorn aegisqa.main:_app --factory --host 0.0.0.0 --port ${PORT}"]
