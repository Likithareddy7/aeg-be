"""JSON Schema -> google.genai `types.Schema`.

Gemini's function-declaration schema is an OpenAPI subset: no
`additionalProperties`, no `$schema`, and nullability is a `nullable` flag
instead of a `["string", "null"]` type union. Same rules the old Node
`normalizeGeminiSchema` applied.
"""

from __future__ import annotations

from typing import Any

from google.genai import types

_DROPPED_KEYS = {"additionalProperties", "$schema", "propertyNames"}


def normalize_json_schema(schema: Any) -> Any:
  if not isinstance(schema, dict):
    return schema
  out: dict[str, Any] = {}
  for key, value in schema.items():
    if key in _DROPPED_KEYS:
      continue
    if key == "type" and isinstance(value, list):
      non_null = next((t for t in value if t != "null"), "string")
      out["type"] = non_null
      if "null" in value:
        out["nullable"] = True
      continue
    if key == "properties" and isinstance(value, dict):
      out["properties"] = {name: normalize_json_schema(sub) for name, sub in value.items()}
      continue
    if key == "items":
      out["items"] = normalize_json_schema(value)
      continue
    out[key] = value
  return out


def to_genai_schema(schema: dict[str, Any]) -> types.Schema:
  normalized = normalize_json_schema(schema)
  return types.Schema.model_validate(_upper_types(normalized))


def _upper_types(schema: Any) -> Any:
  """`types.Schema.type` is an enum of upper-case names (OBJECT, STRING...)."""
  if isinstance(schema, dict):
    return {
      key: (value.upper() if key == "type" and isinstance(value, str) else _upper_types(value))
      for key, value in schema.items()
    }
  if isinstance(schema, list):
    return [_upper_types(item) for item in schema]
  return schema
