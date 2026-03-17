"""Build a retrieval query string from a normalised StoryContext.

Design rules
------------
- Use the *normalized* story only — never the raw Jira JSON.
- Compose a concise natural-language string, not a data dump.
- Field priority (highest → lowest signal):
    1. summary          — the single-sentence intent of the story
    2. acceptance_criteria — the explicit pass/fail conditions (first 300 chars)
    3. components       — Jira component tags (e.g. "Chat Widget")
    4. labels           — Jira labels if present (e.g. "security", "regression")
- Skip components / labels when empty so the query stays clean.
- The result is embedded as RETRIEVAL_QUERY by the caller.

Public API
----------
  build_query(story)  → str  — the query string ready for embed_query()
  build_query_parts(story) → dict[str, str]  — each part separately (for tests/debug)
"""

from __future__ import annotations

import re

from src.models.schemas import StoryContext

# Maximum number of labels to include (they can get noisy quickly)
_MAX_LABELS = 2

# How much of the acceptance criteria text to include in the query
_AC_MAX_CHARS = 300


def _clean(text: str) -> str:
    """Collapse runs of whitespace / newlines into single spaces."""
    return re.sub(r"\s+", " ", text).strip()


def build_query_parts(story: StoryContext) -> dict[str, str]:
    """Return each query fragment as a named dict (useful for inspection/tests).

    Keys: "summary", "acceptance_criteria", "components", "labels"
    Values: cleaned fragment strings (empty string when not applicable).
    """
    # 1. Summary — always present
    summary = _clean(story.summary)

    # 2. Acceptance criteria — trim to first _AC_MAX_CHARS chars
    ac_raw = story.acceptance_criteria or ""
    ac = _clean(ac_raw)[:_AC_MAX_CHARS]

    # 3. Components — join as comma-separated list
    components = ", ".join(c.strip() for c in story.components if c.strip())

    # 4. Labels — top-N non-empty labels
    useful_labels = [lb.strip() for lb in story.labels if lb.strip()][:_MAX_LABELS]
    labels = ", ".join(useful_labels)

    return {
        "summary":             summary,
        "acceptance_criteria": ac,
        "components":          components,
        "labels":              labels,
    }


def build_query(story: StoryContext) -> str:
    """Compose a retrieval query string from a StoryContext.

    The resulting string is suitable for embedding with task_type=RETRIEVAL_QUERY
    and querying against the retrieval_docs ChromaDB collection.

    The format is:
      <summary>. <acceptance_criteria>. [Components: <...>. ] [Tags: <...>.]

    Empty sections are omitted so the query never contains placeholder noise
    like "Components: " or "Tags: ".

    Args:
        story: A normalised StoryContext (from data/normalized/<KEY>.json).

    Returns:
        A single cleaned query string.

    Example:
        >>> build_query(story)
        'Customer Chat Warning Prompt. A brief disclaimer appears when the chat
        window opens. The message is highly visible and placed above the text
        input box or as the first automated greeting message. Components: Chat
        Widget. Tags: security.'
    """
    parts = build_query_parts(story)

    segments: list[str] = []

    if parts["summary"]:
        segments.append(parts["summary"].rstrip("."))

    if parts["acceptance_criteria"]:
        segments.append(parts["acceptance_criteria"].rstrip("."))

    if parts["components"]:
        segments.append(f"Components: {parts['components']}")

    if parts["labels"]:
        segments.append(f"Tags: {parts['labels']}")

    # Join with period-space; terminate with a single period
    return ". ".join(segments) + ("." if segments else "")
