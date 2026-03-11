import base64
import httpx
from typing import Any

from src.config import get_settings


# The only Jira fields we ever need — keeps the response payload small.
_FIELDS = ",".join([
    "summary",
    "description",
    "labels",
    "components",
    "issuelinks",
    "issuetype",
    "status",
    # custom field name for acceptance criteria varies per instance;
    # common names are listed here — ingestor.py picks whichever is present
    "customfield_10016",   # common AC field (Story Points on some, AC on others)
    "customfield_10014",   # Epic Link on older Jira instances
])


class JiraClient:
    """Thin async wrapper around the Jira REST API v3.

    Credentials are read from settings — nothing is hardcoded or passed by callers.
    Only the fields listed in _FIELDS are fetched; Jira skips everything else.
    """

    def __init__(self) -> None:
        s = get_settings()
        self.base_url = s.jira_base_url.rstrip("/")
        credentials = base64.b64encode(
            f"{s.jira_email}:{s.jira_api_token.get_secret_value()}".encode()
        ).decode()
        self._headers = {
            "Authorization": f"Basic {credentials}",
            "Accept": "application/json",
        }

    async def get_issue(self, issue_key: str) -> dict[str, Any]:
        """Fetch a single Jira issue by key.

        Only the fields in _FIELDS are requested — Jira will omit everything else.
        Raises httpx.HTTPStatusError on non-2xx responses.
        """
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{self.base_url}/rest/api/3/issue/{issue_key}",
                headers=self._headers,
                params={"fields": _FIELDS},
            )
            response.raise_for_status()
            return response.json()

    async def create_issue(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a new Jira issue (or subtask) and return the created issue dict.

        The caller is responsible for building the full payload including
        project, issuetype, parent, summary, and description fields.
        Raises httpx.HTTPStatusError on non-2xx responses.
        """
        headers = {**self._headers, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{self.base_url}/rest/api/3/issue",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            return response.json()
