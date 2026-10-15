"""Artifact storage: local disk, Cloud Storage (against a fake client), and
the app serving screenshots and visual-regression images from a non-local
store at the usual URLs."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

import pytest

from aegisqa.config import settings
from aegisqa.storage import GcsArtifactStore, LocalArtifactStore, content_type_for

from .fakes import FakeBrowsers, MemoryArtifactStore, png_base64
from .harness import RunningApp, requires_test_database, sql


async def test_local_store_round_trip_and_path_safety(tmp_path: Path) -> None:
  store = LocalArtifactStore(
    replace(settings, screenshots_dir=tmp_path / "s", artifacts_dir=tmp_path / "a", visual_regression_dir=tmp_path / "v")
  )
  await store.put("visual-regression", "baselines/x.png", b"png")
  assert await store.get("visual-regression", "baselines/x.png") == b"png"
  assert store.local_path("visual-regression", "baselines/x.png") == tmp_path / "v" / "baselines" / "x.png"
  assert await store.get("screenshots", "missing.png") is None
  for bad in ("../secret", "/etc/passwd", "a/../../b"):
    with pytest.raises(ValueError):
      await store.get("screenshots", bad)
  assert content_type_for("v.webm") == "video/webm" and content_type_for("s.png") == "image/png"


class _Blob:
  def __init__(self, bucket: "_Bucket", name: str):
    self._bucket, self.name = bucket, name

  def upload_from_string(self, data: bytes, content_type: str) -> None:
    self._bucket.objects[self.name] = (data, content_type)

  def upload_from_filename(self, filename: str, content_type: str) -> None:
    self._bucket.objects[self.name] = (Path(filename).read_bytes(), content_type)

  def download_as_bytes(self) -> bytes:
    from google.api_core.exceptions import NotFound

    if self.name not in self._bucket.objects:
      raise NotFound("no such object")
    return self._bucket.objects[self.name][0]


class _Bucket:
  def __init__(self) -> None:
    self.objects: dict[str, tuple[bytes, str]] = {}

  def blob(self, name: str) -> _Blob:
    return _Blob(self, name)


class _Client:
  def __init__(self) -> None:
    self.buckets: dict[str, _Bucket] = {}

  def bucket(self, name: str) -> _Bucket:
    return self.buckets.setdefault(name, _Bucket())


async def test_gcs_store_object_names_and_content_types(tmp_path: Path) -> None:
  client = _Client()
  store = GcsArtifactStore("aegisqa-artifacts", client=client)
  await store.put("screenshots", "r1-step1.png", b"img")
  video = tmp_path / "v.webm"
  video.write_bytes(b"vid")
  await store.put_file("artifacts", "run_1/video.webm", video)
  objects = client.buckets["aegisqa-artifacts"].objects
  assert objects == {"screenshots/r1-step1.png": (b"img", "image/png"), "artifacts/run_1/video.webm": (b"vid", "video/webm")}
  assert await store.get("screenshots", "r1-step1.png") == b"img"
  assert await store.get("screenshots", "nope.png") is None
  assert store.local_path("screenshots", "r1-step1.png") is None
  with pytest.raises(ValueError):
    GcsArtifactStore("", client=client)


@pytest.fixture(scope="module")
def app() -> Iterator[dict[str, Any]]:
  from .scripted import AppModel

  store, browsers = MemoryArtifactStore(), FakeBrowsers()
  running = RunningApp(AppModel(model="scripted"), browsers, artifacts=store)
  admin = running.client()
  project = admin.post("/api/projects", json={"name": "Artifacts"}).json()
  yield {"app": running, "admin": admin, "project": project, "store": store, "browsers": browsers}
  running.stop()


@requires_test_database
def test_screenshots_and_visual_regression_through_the_store(app: dict[str, Any]) -> None:
  admin, store, browser = app["admin"], app["store"], app["browsers"].browser
  tc_id = "visual-tc"
  sql(
    'INSERT INTO "TestCase" (id, "projectId", title, objective, preconditions, "testData", "expectedResult", priority, type, status, tags, version, "updatedAt") '
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())",
    (tc_id, app["project"]["id"], "Home looks right", "o", "p", "{}", "e", "low", "ui", "approved", "[]", 1),
  )
  sql(
    'INSERT INTO "TestStep" (id, "testCaseId", "order", action, description, enabled, value) VALUES (%s,%s,%s,%s,%s,%s,%s)',
    ("visual-step", tc_id, 1, "assertVisualMatch", "Compare the page", True, None),
  )

  first = admin.post(f"/api/testcases/{tc_id}/run", json={}).json()
  assert first["status"] == "passed" and "saved as the new baseline" in first["results"][0]["actualResult"]
  assert "visual-regression/baselines/visual-step-baseline.png" in store.objects
  shot = first["results"][0]["screenshots"][0]["filePath"]
  served = admin.get(f"/screenshots/{shot}")
  assert served.status_code == 200 and served.headers["content-type"] == "image/png"
  assert served.content == store.objects[f"screenshots/{shot}"]
  assert admin.get("/screenshots/nothing.png").status_code == 404
  assert admin.get("/screenshots/..%2F..%2Fetc%2Fpasswd").status_code == 404

  assert admin.post(f"/api/testcases/{tc_id}/run", json={}).json()["status"] == "passed"  # identical

  browser._results["browser_take_screenshot"] = {"content": [{"type": "image", "data": png_base64((0, 0, 255)), "mimeType": "image/png"}]}
  changed = admin.post(f"/api/testcases/{tc_id}/run", json={}).json()
  assert changed["status"] == "failed" and "Visual difference detected: 100.000%" in changed["results"][0]["errorMessage"]
  [diff] = admin.get(f"/api/visual-diffs?projectId={app['project']['id']}").json()
  assert admin.get(f"/visual-regression/{diff['diffPath']}").status_code == 200
  assert admin.post(f"/api/visual-diffs/{diff['id']}/approve").json()["status"] == "approved"
  assert store.objects["visual-regression/baselines/visual-step-baseline.png"] == store.objects[f"visual-regression/{diff['actualPath']}"]
  assert admin.post(f"/api/testcases/{tc_id}/run", json={}).json()["status"] == "passed"  # against the new baseline
