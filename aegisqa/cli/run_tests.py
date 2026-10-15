"""CI runner: signs in, runs every approved test case in a project, writes
junit-results.xml, and exits non-zero if anything failed.

  aegisqa-run-tests --project <id> [--environment <id>] --base-url http://localhost:4000 \\
      --username ci-bot --password ...

Every flag has an env var fallback (CI_PROJECT_ID, CI_ENVIRONMENT_ID,
CI_BASE_URL, CI_USERNAME, CI_PASSWORD) so secrets stay out of workflow files.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time

import httpx


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("--project", default=os.environ.get("CI_PROJECT_ID"))
  parser.add_argument("--environment", default=os.environ.get("CI_ENVIRONMENT_ID"))
  parser.add_argument("--base-url", default=os.environ.get("CI_BASE_URL", "http://localhost:4000"))
  parser.add_argument("--username", default=os.environ.get("CI_USERNAME"))
  parser.add_argument("--password", default=os.environ.get("CI_PASSWORD"))
  parser.add_argument("--output", default="junit-results.xml")
  args = parser.parse_args(argv)
  if not (args.project and args.username and args.password):
    parser.error("--project, --username and --password are required (or CI_PROJECT_ID / CI_USERNAME / CI_PASSWORD)")

  # "Run all" blocks until every test case has finished, which can take a while.
  with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=httpx.Timeout(30 * 60, connect=30)) as client:
    print(f"Logging in to {args.base_url} as {args.username}...")
    login = client.post("/api/auth/login", json={"username": args.username, "password": args.password})
    if login.status_code != 200:
      print(f"Login failed: {login.status_code} {login.text}", file=sys.stderr)
      return 1

    print(f"Running all approved test cases for project {args.project}...")
    body = {"environmentId": args.environment} if args.environment else {}
    run_all = client.post(f"/api/projects/{args.project}/testcases/run-all", json=body)
    if run_all.status_code >= 400:
      print(f"run-all failed: {run_all.status_code} {run_all.text}", file=sys.stderr)
      return 1
    batch = run_all.json()
    # With queued execution (TASK_QUEUE=pubsub) runs are still pending here; wait for them.
    deadline = time.monotonic() + 60 * 60
    pending = set(batch["testRunIds"])
    while pending and time.monotonic() < deadline:
      for run_id in list(pending):
        if client.get(f"/api/testruns/{run_id}").json().get("status") not in ("pending", "running"):
          pending.discard(run_id)
      if pending:
        time.sleep(5)
    if pending:
      print(f"Timed out waiting for {len(pending)} run(s).", file=sys.stderr)
      return 1
    print(f"Batch {batch['batchId']} finished with {len(batch['testRunIds'])} run(s). Fetching JUnit report...")

    junit = client.get(f"/api/testruns/batch/{batch['batchId']}/junit")
    if junit.status_code >= 400:
      print(f"Failed to fetch JUnit report: {junit.status_code} {junit.text}", file=sys.stderr)
      return 1
    with open(args.output, "w", encoding="utf-8") as handle:
      handle.write(junit.text)
    print(f"Wrote {args.output}")

  if re.search(r"<failure|<error", junit.text):
    print(f"One or more test cases failed -- see {args.output} for details.", file=sys.stderr)
    return 1
  print("All test cases passed.")
  return 0


if __name__ == "__main__":
  sys.exit(main())
