"""Assembles and persists the ContextPackage for a given story.

Takes normalized ContextItems (from the normalizer) and a normalized
StoryContext (from the ingestor) and combines them into a ContextPackage
that the prompt builder can consume directly.

Single responsibility: assemble + derive coverage hints + save to disk.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.models.schemas import ContextItem, ContextItemType, ContextPackage, StoryContext

# Where context packages are saved
_CONTEXT_DIR = Path(__file__).resolve().parents[2] / "data" / "context"


def _derive_coverage_hints(
    story: StoryContext,
    linked: list[ContextItem],
    jql: list[ContextItem],
) -> list[str]:
    """Infer lightweight coverage hints from already-present context.

    Rules (Day 3):
    - If a TestCase ContextItem's summary mentions an AC tag, note it as covered.
    - If a Bug ContextItem's summary mentions an AC tag, flag it as a known failure area.
    - No LLM calls here — pure string heuristics.
    """
    hints: list[str] = []
    all_items = linked + jql

    for item in all_items:
        # Look for AC-N references in summary
        for token in item.summary.split():
            token_clean = token.strip(".,;:()")
            if token_clean.upper().startswith("AC-") and token_clean[3:].isdigit():
                if item.category == ContextItemType.TEST:
                    hints.append(f"{token_clean} already covered by {item.key} ({item.issue_type})")
                elif item.category == ContextItemType.BUG:
                    hints.append(f"{token_clean} has a known defect: {item.key} — {item.summary}")

    # If linked bugs exist, always add a general hint
    linked_bugs = [i for i in linked if i.category == ContextItemType.BUG]
    if linked_bugs:
        keys = ", ".join(i.key for i in linked_bugs)
        hints.append(f"Known linked defects for this story: {keys} — consider regression and negative tests")

    return list(dict.fromkeys(hints))  # dedupe while preserving order


def build_context_package(
    story: StoryContext,
    linked: list[ContextItem],
    jql: list[ContextItem],
) -> ContextPackage:
    """Assemble a ContextPackage from normalized inputs.

    Routing:
    - Linked items are split into defects vs. tests vs. related stories.
    - JQL items (same label/component) are treated as historical tests / stories.
    - Coverage hints are derived from all items combined.
    """
    linked_defects = [i for i in linked if i.category == ContextItemType.BUG]
    linked_tests   = [i for i in linked if i.category == ContextItemType.TEST]
    linked_stories = [i for i in linked if i.category == ContextItemType.STORY]

    # JQL results are historical — bugs become defects, tests stay as tests
    jql_defects = [i for i in jql if i.category == ContextItemType.BUG]
    jql_tests   = [i for i in jql if i.category in (ContextItemType.TEST, ContextItemType.OTHER)]
    jql_stories = [i for i in jql if i.category == ContextItemType.STORY]

    coverage_hints = _derive_coverage_hints(story, linked, jql)

    return ContextPackage(
        story_key=story.issue_key,
        linked_defects=linked_defects + jql_defects,
        historical_tests=linked_tests + jql_tests,
        related_stories=linked_stories + jql_stories,
        coverage_hints=coverage_hints,
    )


def save_context_package(package: ContextPackage) -> Path:
    """Persist a ContextPackage as JSON under data/context/<story_key>.json."""
    _CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    path = _CONTEXT_DIR / f"{package.story_key}.json"
    path.write_text(package.model_dump_json(indent=2))
    return path
