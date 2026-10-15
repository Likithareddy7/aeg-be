"""Compiles a saved test case into a standalone `@playwright/test` spec --
deterministically, without an LLM or a browser -- and packages approved test
cases as a runnable CI project (zip)."""

from __future__ import annotations

import io
import json
import re
import zipfile
from typing import Any, Optional

_ENV = re.compile(r"\{\{\s*env\.([\w-]+)\s*\}\}")
_PLACEHOLDER = re.compile(r"\{\{\s*(?:env\.([\w-]+)|data\.([\w-]+))\s*\}\}")
PLAYWRIGHT_TEST_VERSION = "^1.48.0"


def js(value: str) -> str:
  """A JavaScript string literal (same output as JSON.stringify)."""
  return json.dumps(value, ensure_ascii=False)


def file_name_for(title: str) -> str:
  return re.sub(r"[^a-z0-9]+", "-", title, flags=re.IGNORECASE).lower() + ".spec.ts"


def collect_env_keys(test_case: dict[str, Any]) -> list[str]:
  """Every `{{env.KEY}}` a test case references -- the standalone run must
  supply these as environment variables."""
  keys: list[str] = []
  for step in test_case["steps"]:
    for match in _ENV.finditer(step.get("value") or ""):
      if match.group(1) not in keys:
        keys.append(match.group(1))
  return keys


def _column_slug(column: str) -> str:
  return re.sub(r"[^a-zA-Z0-9]+", "_", column).upper()


def _data_secret_env_var(row_index: int, column: str) -> str:
  return f"DATA_SECRET_{row_index}_{_column_slug(column)}"


def collect_data_secret_env_keys(data_set: Optional[dict[str, Any]]) -> list[str]:
  if not data_set:
    return []
  return [_data_secret_env_var(i, col) for i in range(len(data_set["rows"])) for col in data_set["secretColumns"]]


def _value_expression(value: str, has_data_set: bool) -> str:
  """A JS expression for a step value: `{{env.KEY}}` becomes
  `process.env.KEY`, `{{data.COL}}` becomes `row["COL"]` inside a
  data-driven loop; everything else stays a string literal."""
  if not _PLACEHOLDER.search(value):
    return js(value)
  parts: list[str] = []
  last = 0
  for match in _PLACEHOLDER.finditer(value):
    if match.start() > last:
      parts.append(js(value[last : match.start()]))
    env_key, data_key = match.group(1), match.group(2)
    if env_key:
      parts.append(f"process.env.{env_key}")
    elif data_key and has_data_set:
      parts.append(f"row[{js(data_key)}]")
    else:
      parts.append(js(match.group(0)))
    last = match.end()
  if last < len(value):
    parts.append(js(value[last:]))
  return parts[0] if len(parts) == 1 else f"({' + '.join(parts)})"


def _indent(code: str) -> str:
  return "\n".join(f"  {line}" for line in code.split("\n"))


def _escape_for_comment(text: str) -> str:
  return text.replace("'", "\\'")


def _escape_for_regex(value: str) -> str:
  return re.sub(r"([.*+?^${}()|\[\]\\/])", r"\\\1", value)


def _locator(selector: dict[str, Any]) -> str:
  strategy = selector.get("strategy")
  description = selector.get("description", "")
  if strategy == "role":
    role = js(selector.get("role") or "generic")
    return f"page.getByRole({role}, {{ name: {js(selector['name'])} }})" if selector.get("name") else f"page.getByRole({role})"
  if strategy == "label":
    return f"page.getByLabel({js(selector.get('label') or description)})"
  if strategy == "placeholder":
    return f"page.getByPlaceholder({js(selector.get('placeholder') or description)})"
  if strategy == "text":
    return f"page.getByText({js(selector.get('name') or description)})"
  if strategy == "altText":
    return f"page.getByAltText({js(selector.get('altText') or description)})"
  if strategy == "testId":
    return f"page.getByTestId({js(selector.get('testId') or description)})"
  if strategy == "xpath":
    return f"page.locator({js('xpath=' + (selector.get('xpath') or ''))})"
  return f"page.locator({js(selector.get('css') or description)})"


_AMBIGUITY = {
  "text": "NOTE: getByText() matches every element with this text, not just one -- if Playwright throws a strict-mode violation here, narrow it with .first()/.nth() or an additional filter.",
  "css": "NOTE: this CSS selector isn't guaranteed unique -- if Playwright throws a strict-mode violation here, narrow it with .first()/.nth() or a more specific selector.",
  "xpath": "NOTE: this XPath isn't guaranteed unique -- if Playwright throws a strict-mode violation here, narrow it with .first()/.nth() or a more specific expression.",
}


def _form_valid_code(locator: Optional[str], value: str) -> str:
  expected_raw, _, rest = (value or "valid").partition(":")
  expected_valid = "true" if expected_raw.strip().lower() != "invalid" else "false"
  lines = [
    f"await expect.poll(() => {locator}.evaluate((el) => (typeof el.checkValidity === 'function' ? el.checkValidity() : true))).toBe({expected_valid});"
  ]
  if rest.strip():
    lines.append(f"await expect.poll(() => {locator}.evaluate((el) => el.validationMessage || '')).toContain({js(rest.strip())});")
  return "\n".join(lines)


def _api_response_code(value: str) -> str:
  criteria = None
  try:
    parsed = json.loads(value)
    if isinstance(parsed.get("urlPattern"), str) and isinstance(parsed.get("expectedStatus"), (int, float)):
      criteria = parsed
  except (ValueError, AttributeError):
    pass
  if criteria is None:
    return f'// TODO: assertApiResponse requires a value like {{"urlPattern": "/api/login", "expectedStatus": 200}}, got: {js(value)}'
  status = criteria["expectedStatus"]
  status_text = str(int(status)) if float(status).is_integer() else str(status)
  return (
    "await expect.poll(async () => {\n"
    f"  const res = await page.waitForResponse((r) => r.url().includes({js(criteria['urlPattern'])}), {{ timeout: 5000 }}).catch(() => null);\n"
    "  return res ? res.status() : null;\n"
    f"}}).toBe({status_text});"
  )


def _accessible_code(locator: Optional[str]) -> str:
  return (
    f"await expect.poll(() => {locator}.evaluate((element) => {{\n"
    "  const tag = element.tagName.toLowerCase();\n"
    "  const ariaLabel = element.getAttribute('aria-label') || '';\n"
    "  const labelledBy = element.getAttribute('aria-labelledby');\n"
    "  let labelledText = '';\n"
    "  if (labelledBy) { labelledText = labelledBy.split(' ').map((id) => { const n = document.getElementById(id); return n ? n.textContent : ''; }).join(' '); }\n"
    "  let name = '';\n"
    "  if (tag === 'img') { name = element.getAttribute('alt') || ''; }\n"
    "  else if (tag === 'input' || tag === 'select' || tag === 'textarea') { const lbl = (element.labels && element.labels.length > 0) ? element.labels[0].textContent : ''; name = ariaLabel || labelledText || lbl || ''; }\n"
    "  else { name = ariaLabel || labelledText || element.textContent || ''; }\n"
    "  return !!name.trim();\n"
    "})).toBe(true);"
  )


def _is_number(value: str) -> bool:
  try:
    number = float(value)
  except ValueError:
    return False
  return number == number and number not in (float("inf"), float("-inf"))


def _step_code(step: dict[str, Any], has_data_set: bool) -> str:
  value = step.get("value") or ""
  locator = _locator(step["selector"]) if step.get("selector") else None
  action = step["action"]
  if action == "navigate":
    return f"await page.goto({_value_expression(value, has_data_set)});"
  if action in ("click", "check", "uncheck", "hover"):
    return f"await {locator}.{action}();"
  if action == "fill":
    return f"await {locator}.fill({_value_expression(value, has_data_set)});"
  if action == "select":
    return f"await {locator}.selectOption({_value_expression(value, has_data_set)});"
  if action == "press":
    return f"await page.keyboard.press({js(value or 'Enter')});"
  if action == "waitFor":
    if value and _is_number(value):
      number = float(value)
      return f"await page.waitForTimeout({int(number) if number.is_integer() else number});"
    return f"await expect(page.getByText({_value_expression(value, has_data_set)})).toBeVisible();"
  if action == "assertVisible":
    return f"await expect({locator}).toBeVisible();"
  if action == "assertText":
    return f"await expect(page.locator('body')).toContainText({_value_expression(value, has_data_set)});"
  if action == "assertUrl":
    return f"await expect(page).toHaveURL(/{_escape_for_regex(value)}/);"
  if action == "screenshot":
    return f"await page.screenshot({{ path: 'step-{step['order']}.png' }});"
  if action == "assertEnabled":
    return f"await expect({locator}).toBeEnabled();"
  if action == "assertDisabled":
    return f"await expect({locator}).toBeDisabled();"
  if action == "assertTableContains":
    return f"await expect({locator}).toContainText({_value_expression(value, has_data_set)});"
  if action == "assertFormValid":
    return _form_valid_code(locator, value)
  if action == "assertApiResponse":
    return _api_response_code(value)
  if action == "assertAccessible":
    return _accessible_code(locator)
  if action == "assertVisualMatch":
    # Visual baselines live in this app's storage and aren't portable; the
    # export captures a screenshot instead. Use Playwright's own
    # toHaveScreenshot() for automated visual checks in the exported project.
    return f"// Visual-regression baseline not exported -- see comment above.\nawait page.screenshot({{ path: 'step-{step['order']}-visual.png' }});"
  return f'// TODO: unsupported action "{action}"'


def _data_array_lines(data_set: dict[str, Any]) -> list[str]:
  """Secret columns become process.env references, never literals, so the
  exported file is safe to commit."""
  secret = set(data_set["secretColumns"])
  rows = []
  for i, row in enumerate(data_set["rows"]):
    fields = [
      f"{js(col)}: {'process.env.' + _data_secret_env_var(i, col) if col in secret else js(row.get(col, ''))}"
      for col in data_set["columns"]
    ]
    rows.append(f"  {{ {', '.join(fields)} }},")
  return ["const DATA = [", "\n".join(rows), "];"]


def export_test_case(test_case: dict[str, Any], data_set: Optional[dict[str, Any]] = None) -> str:
  has_data_set = bool(data_set and data_set["rows"])
  body_lines: list[str] = []
  for step in sorted(test_case["steps"], key=lambda s: s["order"]):
    if not step.get("enabled", True):
      body_lines += [f"// Step {step['order']} (disabled, skipped): {step['description']}", ""]
      continue
    body_lines.append(f"// Step {step['order']}: {step['description']}")
    warning = _AMBIGUITY.get((step.get("selector") or {}).get("strategy", ""))
    if warning:
      body_lines.append(f"// {warning}")
    body_lines += [_step_code(step, has_data_set), ""]
  body = "\n".join(body_lines)

  lines = [
    "import { test, expect } from '@playwright/test';",
    "",
    f"// {test_case['title']}",
    f"// Objective: {test_case['objective']}",
    f"// Preconditions: {test_case['preconditions']}",
    f"// Priority: {test_case['priority']} | Type: {test_case['type']}",
  ]
  title = _escape_for_comment(test_case["title"])
  if has_data_set:
    assert data_set is not None
    lines += _data_array_lines(data_set)
    lines += [
      "",
      "for (const [rowIndex, row] of DATA.entries()) {",
      _indent(f"test(`{title} [row ${{rowIndex}}]`, async ({{ page }}) => {{"),
      _indent(_indent(body)),
      _indent("});"),
      "}",
    ]
  else:
    lines += [f"test('{title}', async ({{ page }}) => {{", _indent(body), "});"]
  return "\n".join(lines)


_PLAYWRIGHT_CONFIG = """import { defineConfig } from '@playwright/test';

// No global `use.baseURL` is set here -- each exported step's own `navigate`
// action already carries an absolute (or process.env-substituted) URL.
export default defineConfig({
  testDir: './tests',
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    trace: 'retain-on-failure',
  },
});
"""


def _readme(test_count: int, env_keys: list[str]) -> str:
  if env_keys:
    env_section = (
      "This export references the following environment variables -- set them before running:\n\n"
      "- from `{{env.KEY}}` credential placeholders in the original test steps\n"
      "- from any secret-flagged columns in a test case's data set (`DATA_SECRET_<row>_<COLUMN>`)\n\n"
      + "\n".join(f"- `{key}`" for key in env_keys)
      + "\n"
    )
  else:
    env_section = "This export does not reference any `{{env.KEY}}` credential placeholders or secret data-set columns.\n"
  return f"""# AegisQA CI export

{test_count} approved test case(s), exported as a standalone `@playwright/test` project.

## Setup

```bash
npm install
npx playwright install --with-deps
npm test
```

## Environment variables

{env_section}"""


def build_ci_package(test_cases: list[dict[str, Any]], data_sets: list[Optional[dict[str, Any]]]) -> bytes:
  """A zip with one spec per approved test case, plus config, package.json
  and a README listing the environment variables the run needs."""
  env_keys: set[str] = set()
  for tc in test_cases:
    env_keys.update(collect_env_keys(tc))
  for ds in data_sets:
    env_keys.update(collect_data_secret_env_keys(ds))

  package_json = json.dumps(
    {
      "name": "aegisqa-ci-export",
      "private": True,
      "scripts": {"test": "playwright test"},
      "devDependencies": {"@playwright/test": PLAYWRIGHT_TEST_VERSION},
    },
    indent=2,
  ) + "\n"

  buffer = io.BytesIO()
  with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for tc, ds in zip(test_cases, data_sets):
      archive.writestr(f"tests/{file_name_for(tc['title'])}", export_test_case(tc, ds))
    archive.writestr("playwright.config.ts", _PLAYWRIGHT_CONFIG)
    archive.writestr("package.json", package_json)
    archive.writestr("README.md", _readme(len(test_cases), sorted(env_keys)))
  return buffer.getvalue()
