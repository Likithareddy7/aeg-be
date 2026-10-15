"""Parsing CSV/JSON test data for data-driven runs."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass


class TestDataParseError(ValueError):
  pass


@dataclass
class ParsedTestData:
  columns: list[str]
  rows: list[dict[str, str]]


def parse_csv(text: str) -> ParsedTestData:
  """RFC 4180: quoted fields with embedded commas/newlines and "" escapes,
  CRLF or LF line endings, first row is the header."""
  rows = [row for row in csv.reader(io.StringIO(text)) if row and row != [""]]
  if len(rows) < 2:
    raise TestDataParseError("CSV must have a header row plus at least one data row.")
  header, *data = rows
  columns = [c.strip() for c in header if c.strip()]
  if not columns:
    raise TestDataParseError("CSV header row has no column names.")
  return ParsedTestData(columns, [{col: (row[i] if i < len(row) else "") for i, col in enumerate(columns)} for row in data])


def _cell(value: object) -> str:
  if value is None:
    return ""
  if isinstance(value, str):
    return value
  return json.dumps(value, separators=(",", ":"))


def parse_json(text: str) -> ParsedTestData:
  """A non-empty array of flat objects. Columns follow first appearance across
  all rows; missing values become ""."""
  try:
    parsed = json.loads(text)
  except ValueError as err:
    raise TestDataParseError(f"Invalid JSON: {err}") from err
  if not isinstance(parsed, list) or not parsed:
    raise TestDataParseError("JSON test data must be a non-empty array of objects.")
  columns: list[str] = []
  for item in parsed:
    if not isinstance(item, dict):
      raise TestDataParseError("Every element of the JSON array must be a flat object.")
    for key in item:
      if key not in columns:
        columns.append(key)
  return ParsedTestData(columns, [{col: _cell(item.get(col)) for col in columns} for item in parsed])


def parse_test_data(text: str, fmt: str) -> ParsedTestData:
  return parse_csv(text) if fmt == "csv" else parse_json(text)
