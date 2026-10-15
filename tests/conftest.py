"""Shared setup for the in-process tests.

Tests that need a database use TEST_DATABASE_URL (a PostgreSQL database whose
name contains "test" -- its schema is dropped and recreated). Without it,
those tests are skipped; everything else runs with no database at all.
"""

from __future__ import annotations

import os
import secrets
import tempfile

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

# Must happen before anything imports aegisqa.config (settings are read once).
_scratch = tempfile.mkdtemp(prefix="aegisqa-tests-")
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
os.environ["SCREENSHOTS_DIR"] = os.path.join(_scratch, "screenshots")
os.environ["ARTIFACTS_DIR"] = os.path.join(_scratch, "artifacts")
os.environ["VISUAL_REGRESSION_DIR"] = os.path.join(_scratch, "visual-regression")
os.environ["WEBHOOK_URL"] = ""
# Used by the queued-execution tests (TASK_QUEUE stays "inline" by default).
os.environ["INTERNAL_AUDIENCE"] = "https://backend.test"
os.environ["INTERNAL_INVOKERS"] = "worker@test.iam"
os.environ["PUBSUB_RUNS_TOPIC"] = "projects/test/topics/runs"
