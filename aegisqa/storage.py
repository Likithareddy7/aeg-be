"""Where screenshots, traces, videos and visual-regression images live.

  ARTIFACT_STORAGE=local  files under SCREENSHOTS_DIR / ARTIFACTS_DIR / VISUAL_REGRESSION_DIR (default)
  ARTIFACT_STORAGE=gcs    objects in ARTIFACTS_BUCKET, as <area>/<path>

Three areas, each served by the backend at the same URL either way:
/screenshots/*, /artifacts/* (traces and videos) and /visual-regression/*.
With Cloud Storage the backend streams objects using its service account, so
no signed URLs or public bucket are needed.
"""

from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path, PurePosixPath
from typing import Optional, Protocol

from .config import Settings

AREAS = ("screenshots", "artifacts", "visual-regression")


def _safe(path: str) -> str:
  """Rejects absolute paths and `..` so a URL can't escape its area."""
  pure = PurePosixPath(path)
  if pure.is_absolute() or any(part in ("..", "") for part in pure.parts):
    raise ValueError(f"Invalid artifact path: {path!r}")
  return pure.as_posix()


def content_type_for(path: str) -> str:
  if path.endswith(".webm"):
    return "video/webm"
  if path.endswith(".trace") or path.endswith(".zip"):
    return "application/zip"
  return mimetypes.guess_type(path)[0] or "application/octet-stream"


class ArtifactStore(Protocol):
  async def put(self, area: str, path: str, data: bytes) -> None: ...
  async def get(self, area: str, path: str) -> Optional[bytes]: ...
  async def put_file(self, area: str, path: str, local: Path) -> None: ...
  def local_path(self, area: str, path: str) -> Optional[Path]: ...


class LocalArtifactStore:
  def __init__(self, settings: Settings):
    self._roots = {
      "screenshots": settings.screenshots_dir,
      "artifacts": settings.artifacts_dir,
      "visual-regression": settings.visual_regression_dir,
    }

  def _file(self, area: str, path: str) -> Path:
    return self._roots[area] / _safe(path)

  async def put(self, area: str, path: str, data: bytes) -> None:
    target = self._file(area, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

  async def get(self, area: str, path: str) -> Optional[bytes]:
    target = self._file(area, path)
    return target.read_bytes() if target.is_file() else None

  async def put_file(self, area: str, path: str, local: Path) -> None:
    target = self._file(area, path)
    if target.resolve() != local.resolve():
      await self.put(area, path, local.read_bytes())

  def local_path(self, area: str, path: str) -> Optional[Path]:
    target = self._file(area, path)
    return target if target.is_file() else None


class GcsArtifactStore:
  def __init__(self, bucket: str, project: str = "", client: object = None):
    if not bucket:
      raise ValueError("ARTIFACT_STORAGE=gcs needs ARTIFACTS_BUCKET")
    if client is None:
      from google.cloud import storage

      client = storage.Client(project=project or None)
    self._bucket = client.bucket(bucket)  # type: ignore[attr-defined]

  def _blob(self, area: str, path: str):  # type: ignore[no-untyped-def]
    if area not in AREAS:
      raise ValueError(f"Unknown artifact area {area!r}")
    return self._bucket.blob(f"{area}/{_safe(path)}")

  async def put(self, area: str, path: str, data: bytes) -> None:
    blob = self._blob(area, path)
    await asyncio.to_thread(blob.upload_from_string, data, content_type=content_type_for(path))

  async def get(self, area: str, path: str) -> Optional[bytes]:
    from google.api_core.exceptions import NotFound

    blob = self._blob(area, path)
    try:
      return await asyncio.to_thread(blob.download_as_bytes)
    except NotFound:
      return None

  async def put_file(self, area: str, path: str, local: Path) -> None:
    blob = self._blob(area, path)
    await asyncio.to_thread(blob.upload_from_filename, str(local), content_type=content_type_for(path))

  def local_path(self, area: str, path: str) -> Optional[Path]:
    return None


def build_artifact_store(settings: Settings) -> ArtifactStore:
  if settings.artifact_storage == "gcs":
    return GcsArtifactStore(settings.artifacts_bucket, settings.google_cloud_project)
  if settings.artifact_storage != "local":
    raise ValueError(f"Unknown ARTIFACT_STORAGE {settings.artifact_storage!r} (use local or gcs)")
  return LocalArtifactStore(settings)
