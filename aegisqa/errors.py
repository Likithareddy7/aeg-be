"""API errors and request-body validation.

Every error response is `{"error": "<message>"}` with the status code the
frontend expects (400 for bad input, not FastAPI's default 422)."""

from __future__ import annotations

from typing import Any, Optional, TypeVar

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

ModelT = TypeVar("ModelT", bound=BaseModel)


class ApiError(Exception):
  def __init__(self, status: int, message: str):
    super().__init__(message)
    self.status = status
    self.message = message


def not_found(message: str) -> ApiError:
  return ApiError(404, message)


def forbidden(message: str) -> ApiError:
  return ApiError(403, message)


def bad_request(message: str) -> ApiError:
  return ApiError(400, message)


def describe_validation_error(err: ValidationError) -> str:
  parts = []
  for issue in err.errors():
    location = ".".join(str(p) for p in issue.get("loc", ()) if p != "__root__")
    parts.append(f"{location}: {issue['msg']}" if location else issue["msg"])
  return "; ".join(parts) or "Invalid request body."


def parse_body(model: type[ModelT], body: Any, message: Optional[str] = None) -> ModelT:
  """Validates a raw JSON body. `message` replaces the generated description
  for routes whose original API returned a fixed error text."""
  try:
    return model.model_validate(body if body is not None else {})
  except ValidationError as err:
    raise ApiError(400, message or describe_validation_error(err)) from err


async def api_error_handler(_: Request, exc: Exception) -> JSONResponse:
  assert isinstance(exc, ApiError)
  return JSONResponse({"error": exc.message}, status_code=exc.status)
