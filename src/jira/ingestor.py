"""Converts raw Jira API payloads into StoryContext domain models.

Nothing in here makes HTTP calls — it only parses what the client already fetched.
All consumers (Gemini, DeepEval, the API) must use StoryContext — never raw Jira dicts.
"""

import re
from typing import Any

from src.models.schemas import LinkedIssue, StoryContext


# ── ADF → plain text ──────────────────────────────────────────────────────────

def _adf_to_text(adf: dict[str, Any] | None) -> str | None:
    """Flatten Atlassian Document Format (ADF) into plain text.

    Handles the node types used by Jira Cloud's description field.
    Returns None if the input is empty or produces no text.
    """
    if not adf:
        return None
    parts: list[str] = []

    def _walk(node: dict[str, Any]) -> None:
        node_type = node.get("type", "")
        if node_type == "text":
            parts.append(node.get("text", ""))
        elif node_type in ("hardBreak", "rule"):
            parts.append("\n")
        for child in node.get("content", []):
            _walk(child)
        if node_type in ("paragraph", "heading", "bulletList", "orderedList", "listItem"):
            parts.append("\n")

    _walk(adf)
    return "".join(parts).strip() or None


# ── Acceptance criteria extraction ────────────────────────────────────────────

# Known custom field IDs for acceptance criteria across common Jira configurations.
_AC_FIELD_CANDIDATES = [
    "customfield_10016",
    "customfield_10014",
    "customfield_10500",
    "customfield_10501",
]

# Regex that matches inline AC headings in a description block.
_AC_HEADING_RE = re.compile(
    r"(?:acceptance criteria|ac|criteria)\s*:\s*(.+)",
    re.IGNORECASE | re.DOTALL,
)


def _extract_acceptance_criteria(fields: dict[str, Any], plain_description: str | None) -> str | None:
    """Return acceptance criteria text, or None if not found.

    Priority:
      1. Dedicated Jira custom field (various IDs across instances)
      2. Inline section in the description text
    """
    # 1. Dedicated custom field
    for field_id in _AC_FIELD_CANDIDATES:
        value = fields.get(field_id)
        if not value:
            continue
        if isinstance(value, str):
            return value.strip() or None
        if isinstance(value, dict) and value.get("type") == "doc":
            return _adf_to_text(value)

    # 2. Inline in description
    if plain_description:
        match = _AC_HEADING_RE.search(plain_description)
        if match:
            return match.group(1).strip() or None

    return None


def _strip_ac_from_description(description: str | None, acceptance_criteria: str | None) -> str | None:
    """Remove the AC section from the description so the two fields don't repeat.

    Only strips when AC was extracted inline — custom-field AC is already separate.
    """
    if not description or not acceptance_criteria:
        return description
    match = _AC_HEADING_RE.search(description)
    if not match:
        return description
    before = description[: match.start()].strip()
    return before or None


# ── Public API ────────────────────────────────────────────────────────────────

def parse_issue(raw: dict[str, Any]) -> StoryContext:
    """Parse a raw Jira REST API v3 issue payload into a StoryContext.

    This is the single entry point for all normalization.
    After this function returns, no other code should ever read raw Jira dicts.

    Args:
        raw: The JSON dict returned by JiraClient.get_issue().

    Returns:
        A validated StoryContext ready for use as LLM input.
    """
    issue_key: str = raw["key"]
    fields: dict[str, Any] = raw.get("fields", {})

    summary: str = fields.get("summary", "").strip()

    # Flatten ADF → plain text first
    full_description = _adf_to_text(fields.get("description"))

    # Extract AC (from custom field or inline), then strip it from description
    acceptance_criteria = _extract_acceptance_criteria(fields, full_description)
    description = _strip_ac_from_description(full_description, acceptance_criteria)

    labels: list[str] = [l for l in (fields.get("labels") or []) if l]

    components: list[str] = [
        c["name"] for c in (fields.get("components") or []) if c.get("name")
    ]

    linked_issues: list[LinkedIssue] = []
    seen_linked_keys: set[str] = set()
    for link in fields.get("issuelinks") or []:
        linked_raw = link.get("inwardIssue") or link.get("outwardIssue")
        if not linked_raw:
            continue
        linked_key = linked_raw["key"]
        if linked_key in seen_linked_keys:
            continue
        linked_issues.append(
            LinkedIssue(
                key=linked_key,
                issue_type=linked_raw.get("fields", {}).get("issuetype", {}).get("name", "Unknown"),
                summary=linked_raw.get("fields", {}).get("summary", ""),
            )
        )
        seen_linked_keys.add(linked_key)

    for subtask in fields.get("subtasks") or []:
        subtask_key = subtask.get("key")
        if not subtask_key or subtask_key in seen_linked_keys:
            continue
        subtask_fields = subtask.get("fields", {})
        linked_issues.append(
            LinkedIssue(
                key=subtask_key,
                issue_type=subtask_fields.get("issuetype", {}).get("name", "Sub-task"),
                summary=subtask_fields.get("summary", ""),
            )
        )
        seen_linked_keys.add(subtask_key)

    return StoryContext(
        issue_key=issue_key,
        summary=summary,
        description=description,
        acceptance_criteria=acceptance_criteria,
        labels=labels,
        components=components,
        linked_issues=linked_issues,
    )
