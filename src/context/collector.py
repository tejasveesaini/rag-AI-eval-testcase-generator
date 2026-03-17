"""Fetches raw related Jira issues for a given story key.

Single responsibility: make HTTP calls, return raw dicts.
No normalization happens here — that is the normalizer's job.

Strategy:
  1. Linked issues   — whatever Jira's issuelinks field contains for the story.
  2. Narrow JQL      — issues sharing a label OR component with the story,
                       limited to bugs and test-cases only, capped at 10 results.
  3. Keyword JQL     — issues whose summary text-matches key nouns extracted
                       from the story summary and acceptance criteria (regardless
                       of label/component overlap). Catches related stories/bugs
                       that share NO metadata tags with the current story.
  4. Broad fallback  — if keyword search returns < _KEYWORD_MIN hits, fall back
                       to all issues in the project updated in the last 90 days
                       across all supported issue types, capped at _BROAD_MAX.

Why not just linked + narrow JQL?
  Labels and components are often missing or inconsistent. Keyword-based search
  covers the "same feature, different ticket" case that pure metadata filtering
  misses entirely.
"""

from __future__ import annotations

import re
import string
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
    "subtasks",
])

# Hard cap on JQL results — we never want more than this in a context package.
_JQL_MAX = 10

# Keyword search limits
_KEYWORD_MAX  = 10   # max results per keyword query
_KEYWORD_MIN  = 3    # if fewer hits, also run broad fallback
_BROAD_MAX    = 8    # max results for the broad fallback pass

# Issue types we consider useful context. Others (epics, service requests) are noise.
_USEFUL_TYPES = {
    "Bug",
    "Defect",
    "TestCase",
    "Test Case",
    "Story",
    "Task",
    "Sub-task",
    "Subtask",
    "New Feature",
}

_CONTEXT_ISSUETYPE_JQL = 'Bug, Defect, TestCase, "Test Case", Story, Task, "Sub-task", "New Feature"'
_NARROW_ISSUETYPE_JQL = 'Bug, Defect, TestCase, "Test Case", "Sub-task"'

# Regex to extract first meaningful sentence from plain text
_FIRST_SENTENCE_RE = re.compile(r"([^.!?\n]{10,}[.!?])")

# Common English stop-words to strip before building keyword clauses
_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "as", "by", "from", "is", "it", "its", "be", "are", "was",
    "were", "has", "have", "had", "do", "does", "did", "will", "would",
    "should", "could", "may", "might", "must", "can", "not", "no", "so",
    "if", "that", "this", "these", "those", "then", "than", "when", "which",
    "who", "whom", "what", "how", "all", "any", "each", "both", "few", "more",
    "most", "other", "some", "such", "only", "own", "same", "than", "too",
    "very", "just", "also",
})


def _normalize_issue_type_name(name: str | None) -> str:
    """Normalize Jira issue type names for loose matching."""
    return re.sub(r"[\s_-]+", "", (name or "").strip()).lower()


def _is_useful_issue_type(name: str | None) -> bool:
    """Return True for issue types that can provide meaningful QA context."""
    normalized = _normalize_issue_type_name(name)
    if not normalized:
        return False

    if name in _USEFUL_TYPES:
        return True

    return normalized in {
        "bug",
        "defect",
        "testcase",
        "subtask",
        "story",
        "task",
        "newfeature",
    }


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
        f"AND issueType in ({_NARROW_ISSUETYPE_JQL}) "
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

        # 2. Resolve directly-related keys from issuelinks + subtasks
        linked_keys: list[str] = []
        for link in issuelinks:
            for direction in ("inwardIssue", "outwardIssue"):
                issue = link.get(direction)
                if issue:
                    linked_keys.append(issue["key"])

        for subtask in fields.get("subtasks", []):
            key = subtask.get("key")
            if key:
                linked_keys.append(key)

        # Deduplicate while preserving order.
        linked_keys = list(dict.fromkeys(linked_keys))

        # Fetch each directly-related issue (linked issue or subtask).
        linked_raw: list[dict[str, Any]] = []
        for key in linked_keys:
            raw = await _fetch_raw(client, base_url, headers, key)
            if raw:
                itype = raw.get("fields", {}).get("issuetype", {}).get("name", "")
                if _is_useful_issue_type(itype):
                    linked_raw.append(raw)

        # 3. Narrow JQL — same label or component, bugs and test-cases only
        jql_raw = await _jql_search(client, base_url, headers, story_key, labels, components)

        return {
            "story_key": story_key,
            "linked_raw": linked_raw,
            "jql_raw": jql_raw,
        }


# ── Keyword-based discovery ────────────────────────────────────────────────────

def _extract_keywords(summary: str, acceptance_criteria: str | None) -> list[str]:
    """Extract high-signal nouns / noun-phrases from story summary + AC text.

    Strategy:
      1. Tokenise the combined text into lowercase words.
      2. Remove punctuation, stop-words, and tokens shorter than 4 chars.
      3. Deduplicate while preserving order.
      4. Cap at 6 keywords — enough for a focused JQL ``summary ~`` clause.

    Returns a list of cleaned keyword strings, e.g. ["chat", "warning", "prompt"].
    """
    combined = f"{summary} {acceptance_criteria or ''}"
    # Strip punctuation
    translator = str.maketrans("", "", string.punctuation)
    cleaned = combined.translate(translator).lower()
    tokens: list[str] = cleaned.split()

    seen: set[str] = set()
    keywords: list[str] = []
    for tok in tokens:
        if tok in _STOP_WORDS or len(tok) < 4 or not tok.isalpha():
            continue
        if tok not in seen:
            seen.add(tok)
            keywords.append(tok)
        if len(keywords) >= 6:
            break

    return keywords


async def _keyword_jql_search(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict,
    story_key: str,
    keywords: list[str],
    exclude_keys: set[str],
) -> list[dict[str, Any]]:
    """Run a Jira text-search JQL using summary ~ keyword clauses.

    Searches all useful issue types (not just bugs/test-cases) so related
    stories and tasks are also discovered.

    The query is:
      project = <PROJECT> AND summary ~ "<kw1>" AND summary ~ "<kw2>"
      AND issue NOT IN (<exclude_keys>) ORDER BY updated DESC

    Falls back gracefully to fewer keywords if the search returns nothing.
    """
    if not keywords:
        return []

    project = story_key.split("-")[0]
    exc_clause = ", ".join(f'"{k}"' for k in exclude_keys) if exclude_keys else f'"{story_key}"'

    # Build keyword clauses — AND them together for precision
    kw_clauses = " AND ".join(f'summary ~ "{kw}"' for kw in keywords[:4])

    jql = (
        f"project = {project} "
        f"AND issueType in ({_CONTEXT_ISSUETYPE_JQL}) "
        f"AND ({kw_clauses}) "
        f"AND issue NOT IN ({exc_clause}) "
        f"ORDER BY updated DESC"
    )

    try:
        r = await client.post(
            f"{base_url}/rest/api/3/issue/search",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "jql": jql,
                "maxResults": _KEYWORD_MAX,
                "fields": _RELATED_FIELDS.split(","),
            },
            timeout=12.0,
        )
        r.raise_for_status()
        results = r.json().get("issues", [])
    except Exception:
        results = []

    # Retry with only the first 2 keywords if we got nothing
    if not results and len(keywords) > 2:
        kw_clauses_narrow = " AND ".join(f'summary ~ "{kw}"' for kw in keywords[:2])
        jql_narrow = (
            f"project = {project} "
            f"AND issueType in ({_CONTEXT_ISSUETYPE_JQL}) "
            f"AND ({kw_clauses_narrow}) "
            f"AND issue NOT IN ({exc_clause}) "
            f"ORDER BY updated DESC"
        )
        try:
            r = await client.post(
                f"{base_url}/rest/api/3/issue/search",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "jql": jql_narrow,
                    "maxResults": _KEYWORD_MAX,
                    "fields": _RELATED_FIELDS.split(","),
                },
                timeout=12.0,
            )
            r.raise_for_status()
            results = r.json().get("issues", [])
        except Exception:
            results = []

    return results


async def _broad_fallback_search(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict,
    story_key: str,
    exclude_keys: set[str],
) -> list[dict[str, Any]]:
    """Broad fallback: recent issues in the same project across all useful types.

    Used when keyword search returns too few results. Sorted by ``updated DESC``
    so the most recently touched issues (most contextually relevant) come first.
    """
    project = story_key.split("-")[0]
    exc_clause = ", ".join(f'"{k}"' for k in exclude_keys) if exclude_keys else f'"{story_key}"'

    jql = (
        f"project = {project} "
        f"AND issueType in ({_CONTEXT_ISSUETYPE_JQL}) "
        f"AND issue NOT IN ({exc_clause}) "
        f"AND updated >= -90d "
        f"ORDER BY updated DESC"
    )

    try:
        r = await client.post(
            f"{base_url}/rest/api/3/issue/search",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "jql": jql,
                "maxResults": _BROAD_MAX,
                "fields": _RELATED_FIELDS.split(","),
            },
            timeout=12.0,
        )
        r.raise_for_status()
        return r.json().get("issues", [])
    except Exception:
        return []


async def search_related_issues(
    story_key: str,
    summary: str,
    acceptance_criteria: str | None,
    already_known_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Discover related Jira issues via keyword + broad-fallback search.

    This is the entry point for the discovery pipeline. It does NOT use
    label/component metadata — only the story's own text — so it finds
    related issues even when metadata tags are absent or inconsistent.

    Pipeline:
      1. Extract keywords from summary + AC.
      2. Run keyword JQL (summary ~ kw1 AND summary ~ kw2 …).
      3. If fewer than _KEYWORD_MIN hits, also run broad-fallback JQL.
      4. Merge results, dedup by key, exclude already_known_keys.

    Args:
        story_key:          The issue key of the story being processed.
        summary:            Story summary text.
        acceptance_criteria: Story AC text, or None.
        already_known_keys: Keys already collected via linked/jql passes;
                            excluded from all result sets.

    Returns:
        List of raw Jira issue dicts (same shape as linked_raw / jql_raw).
    """
    jira = JiraClient()
    base_url = jira.base_url
    headers = jira._headers

    exclude_keys: set[str] = {story_key}
    if already_known_keys:
        exclude_keys.update(already_known_keys)

    keywords = _extract_keywords(summary, acceptance_criteria)

    async with httpx.AsyncClient(timeout=20.0) as client:
        keyword_results = await _keyword_jql_search(
            client, base_url, headers, story_key, keywords, exclude_keys
        )

        # Dedup by key immediately
        seen: set[str] = set(exclude_keys)
        merged: list[dict[str, Any]] = []
        for issue in keyword_results:
            key = issue.get("key", "")
            if key and key not in seen:
                seen.add(key)
                merged.append(issue)

        # Broad fallback if keyword search is thin
        if len(merged) < _KEYWORD_MIN:
            fallback_results = await _broad_fallback_search(
                client, base_url, headers, story_key, seen
            )
            for issue in fallback_results:
                key = issue.get("key", "")
                if key and key not in seen:
                    seen.add(key)
                    merged.append(issue)

    return merged
