"""Converts raw related Jira issue dicts into ContextItems.

Mirrors the pattern in src/jira/ingestor.py:
  raw Jira dict  →  controlled internal model (ContextItem)

Nothing in here makes HTTP calls. It only transforms what the collector fetched.
The goal is to keep only the signal fields and discard all Jira noise.
"""

from __future__ import annotations

import re
from typing import Any

from src.jira.ingestor import _adf_to_text  # reuse ADF flattener
from src.models.schemas import ContextItem, ContextItemType

# Map Jira issue type names to semantic categories used for prompt sectioning
_TYPE_TO_CATEGORY: dict[str, ContextItemType] = {
    "Bug": ContextItemType.BUG,
    "TestCase": ContextItemType.TEST,
    "Sub-task": ContextItemType.TEST,   # TestCase subtasks may show as Sub-task
    "Story": ContextItemType.STORY,
    "New Feature": ContextItemType.STORY,
    "Task": ContextItemType.STORY,
}

# Extract the first meaningful sentence (10+ chars) from any text block
_FIRST_SENTENCE_RE = re.compile(r"([^.!?\n]{10,}[.!?\n])")


def _first_sentence(text: str | None) -> str | None:
    """Return the first meaningful sentence from a plain-text block, or None.

    Skips very short lines (like ADF headings rendered as bare words)
    and prefers content that is actually descriptive (20+ chars).
    """
    if not text:
        return None
    # Split into lines, skip headings / very short lines
    for line in text.splitlines():
        line = line.strip()
        if len(line) >= 20:
            # Truncate at 120 chars
            return line[:120]
    return None


def normalize_related_issue(
    raw: dict[str, Any],
    relevance_hint: str | None = None,
) -> ContextItem | None:
    """Convert one raw Jira issue dict into a ContextItem.

    Returns None if the issue is missing key fields (guards against empty dicts).
    """
    key = raw.get("key")
    fields = raw.get("fields", {})
    if not key or not fields:
        return None

    summary = (fields.get("summary") or "").strip()
    if not summary:
        return None

    issue_type_name = fields.get("issuetype", {}).get("name", "Other")
    category = _TYPE_TO_CATEGORY.get(issue_type_name, ContextItemType.OTHER)

    # Extract short_text: first meaningful sentence from description
    raw_description = fields.get("description")
    plain_description: str | None = None
    if isinstance(raw_description, dict):
        # ADF format from Jira Cloud
        plain_description = _adf_to_text(raw_description)
    elif isinstance(raw_description, str):
        plain_description = raw_description.strip() or None

    short_text = _first_sentence(plain_description)

    return ContextItem(
        key=key,
        issue_type=issue_type_name,
        category=category,
        summary=summary,
        short_text=short_text,
        relevance_hint=relevance_hint,
    )


def normalize_raw_context(raw_context: dict[str, Any]) -> dict[str, list[ContextItem]]:
    """Normalize the full output from collector.collect_raw_context().

    Returns a dict with two lists:
        "linked"  – ContextItems built from linked_raw
        "jql"     – ContextItems built from jql_raw (deduped against linked)

    Deduplication: if the same issue key appears in both sets, it is kept only
    in "linked" (direct link is a stronger signal).
    """
    linked_items: list[ContextItem] = []
    seen_keys: set[str] = set()

    for raw in raw_context.get("linked_raw", []):
        item = normalize_related_issue(raw, relevance_hint="linked issue")
        if item and item.key not in seen_keys:
            linked_items.append(item)
            seen_keys.add(item.key)

    jql_items: list[ContextItem] = []
    for raw in raw_context.get("jql_raw", []):
        item = normalize_related_issue(raw, relevance_hint="same label/component")
        if item and item.key not in seen_keys:
            jql_items.append(item)
            seen_keys.add(item.key)

    return {"linked": linked_items, "jql": jql_items}
