"""Fetches raw related Jira issues for a given story key.

Single responsibility: make HTTP calls, return raw dicts.
No normalization happens here — that is the normalizer's job.

Strategy (deliberately narrow — Day 3):
  1. Linked issues   — whatever Jira's issuelinks field contains for the story.
  2. Narrow JQL      — issues sharing a label OR component with the story,
                       limited to bugs and test-cases only, capped at 10 results.

Why narrow?
  Broad searches (full project, all bugs, all time) add noise that degrades
  generation quality. We only want signal that is directly relevant to this story.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from src.config import get_settings
from src.jira.client import JiraClient

# Fields fetched for related issues — kept minimal to reduce payload size.
_RELATED_FIELDS = ",".join([
    "summary",
    "issuetype",
    "status",
    "description",
    "labels",
    "components",
    "issuelinks",
])

# Hard cap on JQL results — we never want more than this in a context package.
_JQL_MAX = 10

# Issue types we consider useful context. Others (epics, service requests) are noise.
_USEFUL_TYPES = {"Bug", "TestCase", "Story", "Task", "Sub-task", "New Feature"}

# Regex to extract first meaningful sentence from plain text
_FIRST_SENTENCE_RE = re.compile(r"([^.!?\n]{10,}[.!?])")


async def _fetch_raw(client: httpx.AsyncClient, base_url: str, headers: dict, issue_key: str) -> dict[str, Any]:
    """Fetch a single issue with minimal fields. Returns empty dict on error."""
    try:
        r = await client.get(
            f"{base_url}/rest/api/3/issue/{issue_key}",
            headers=headers,
            params={"fields": _RELATED_FIELDS},
            timeout=8.0,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


async def _jql_search(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict,
    story_key: str,
    labels: list[str],
    components: list[str],
) -> list[dict[str, Any]]:
    """Run a narrow JQL query scoped to labels/components of the story.

    Only fetches bugs and test-cases. Excludes the story itself.
    Returns an empty list if no filters are available or the query fails.
    """
    filters: list[str] = []

    if labels:
        label_clause = " OR ".join(f'labels = "{l}"' for l in labels[:2])
        filters.append(f"({label_clause})")

    if components:
        comp_clause = " OR ".join(f'component = "{c}"' for c in components[:2])
        filters.append(f"({comp_clause})")

    if not filters:
        return []

    jql = (
        f"project = {story_key.split('-')[0]} "
        f"AND issueType in (Bug, TestCase) "
        f"AND ({' OR '.join(filters)}) "
        f"AND issue != {story_key} "
        f"ORDER BY updated DESC"
    )

    try:
        r = await client.post(
            f"{base_url}/rest/api/3/issue/search",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "jql": jql,
                "maxResults": _JQL_MAX,
                "fields": _RELATED_FIELDS.split(","),
            },
            timeout=10.0,
        )
        r.raise_for_status()
        return r.json().get("issues", [])
    except Exception:
        return []


async def collect_raw_context(story_key: str) -> dict[str, Any]:
    """Fetch all raw related issues for a story.

    Returns a dict with:
        story_key     – the input key
        linked_raw    – list of raw issue dicts from issuelinks
        jql_raw       – list of raw issue dicts from narrow JQL

    Callers must normalize this before passing it to the prompt.
    """
    jira = JiraClient()
    base_url = jira.base_url
    headers = jira._headers

    async with httpx.AsyncClient(timeout=15.0) as client:
        # 1. Fetch the story itself to get its labels, components, and issuelinks
        story_raw = await _fetch_raw(client, base_url, headers, story_key)
        if not story_raw:
            return {"story_key": story_key, "linked_raw": [], "jql_raw": []}

        fields = story_raw.get("fields", {})
        labels: list[str] = fields.get("labels", [])
        components: list[str] = [c["name"] for c in fields.get("components", [])]
        issuelinks: list[dict] = fields.get("issuelinks", [])

        # 2. Resolve linked issue keys — filter to useful types only
        linked_keys: list[str] = []
        for link in issuelinks:
            for direction in ("inwardIssue", "outwardIssue"):
                issue = link.get(direction)
                if issue:
                    linked_keys.append(issue["key"])

        # Fetch each linked issue (in parallel would be ideal, sequential is fine for Day 3)
        linked_raw: list[dict[str, Any]] = []
        for key in linked_keys:
            raw = await _fetch_raw(client, base_url, headers, key)
            if raw:
                itype = raw.get("fields", {}).get("issuetype", {}).get("name", "")
                if itype in _USEFUL_TYPES:
                    linked_raw.append(raw)

        # 3. Narrow JQL — same label or component, bugs and test-cases only
        jql_raw = await _jql_search(client, base_url, headers, story_key, labels, components)

        return {
            "story_key": story_key,
            "linked_raw": linked_raw,
            "jql_raw": jql_raw,
        }
