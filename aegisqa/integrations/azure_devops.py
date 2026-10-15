"""Azure DevOps (Work Item Tracking REST API, api-version 7.1), authenticated
with a Personal Access Token (Basic auth, empty username).

- create_bug:     file a failed run as a Bug work item

`base_url` is the organization URL (https://dev.azure.com/yourorg) and
`project` the Azure DevOps project name, as saved with PUT /api/projects/{id}/integrations/azureDevOps.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

INTEGRATION_TYPE = "azureDevOps"


@dataclass
class CreatedIssue:
  key: str
  url: str


class AzureDevOpsError(Exception):
  pass


class _Connection:
  def __init__(self, base_url: str, project: str, personal_access_token: str):
    self.organization_url = base_url.rstrip("/")
    self.project_path = quote(project, safe="")
    token = base64.b64encode(f":{personal_access_token}".encode()).decode()
    self.headers = {"Authorization": f"Basic {token}"}

  def url(self, path: str) -> str:
    return f"{self.organization_url}/{self.project_path}/{path}"

  def work_item_page(self, work_item_id: int) -> str:
    return self.url(f"_workitems/edit/{work_item_id}")

  async def request(self, method: str, path: str, **kwargs: Any) -> Any:
    headers = {**self.headers, **kwargs.pop("headers", {})}
    async with httpx.AsyncClient(timeout=30) as client:
      response = await client.request(method, self.url(path), headers=headers, **kwargs)
    if response.status_code >= 400:
      detail = response.text
      try:
        detail = response.json().get("message") or detail
      except ValueError:
        pass
      raise AzureDevOpsError(f"Azure DevOps API error ({response.status_code}): {detail}")
    return response.json() if response.content else None


def _html_from_text(text: str) -> str:
  # Work item rich-text fields render HTML; keep line breaks visible.
  escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
  return escaped.replace("\n", "<br>")


async def create_bug(base_url: str, project: str, personal_access_token: str, title: str, description: str) -> CreatedIssue:
  conn = _Connection(base_url, project, personal_access_token)
  body = await conn.request(
    "POST",
    "_apis/wit/workitems/$Bug?api-version=7.1",
    headers={"Content-Type": "application/json-patch+json"},
    json=[
      {"op": "add", "path": "/fields/System.Title", "value": title},
      {"op": "add", "path": "/fields/System.Description", "value": _html_from_text(description)},
    ],
  )
  # The response's own `url` is the REST resource; link to the browsable page.
  return CreatedIssue(key=f"#{body['id']}", url=conn.work_item_page(body["id"]))

