"""Tests for src/retrieval/query_builder.py.

Coverage:
  build_query_parts  — each field extracted correctly; empty fields produce ""
  build_query        — full string composition; period-separated segments;
                       no trailing noise for missing fields; AC truncation;
                       label capping; whitespace normalisation
"""

from __future__ import annotations

import pytest

from src.models.schemas import StoryContext
from src.retrieval.query_builder import (
    _AC_MAX_CHARS,
    _MAX_LABELS,
    build_query,
    build_query_parts,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_story(
    summary: str = "Customer Chat Warning Prompt",
    description: str | None = "As a customer using the chat, I want a warning.",
    acceptance_criteria: str | None = (
        "A brief disclaimer appears when the chat window opens. "
        "The message is highly visible."
    ),
    labels: list[str] | None = None,
    components: list[str] | None = None,
) -> StoryContext:
    return StoryContext(
        issue_key="AIP-2",
        summary=summary,
        description=description,
        acceptance_criteria=acceptance_criteria,
        labels=labels or [],
        components=components or [],
    )


# ── build_query_parts ─────────────────────────────────────────────────────────

class TestBuildQueryParts:
    def test_summary_always_present(self) -> None:
        parts = build_query_parts(_make_story(summary="My summary"))
        assert parts["summary"] == "My summary"

    def test_summary_whitespace_collapsed(self) -> None:
        parts = build_query_parts(_make_story(summary="  My   summary  "))
        assert parts["summary"] == "My summary"

    def test_acceptance_criteria_extracted(self) -> None:
        parts = build_query_parts(_make_story(acceptance_criteria="AC text here."))
        assert "AC text here" in parts["acceptance_criteria"]

    def test_acceptance_criteria_truncated_at_max(self) -> None:
        long_ac = "x" * (_AC_MAX_CHARS + 50)
        parts = build_query_parts(_make_story(acceptance_criteria=long_ac))
        assert len(parts["acceptance_criteria"]) <= _AC_MAX_CHARS

    def test_acceptance_criteria_none_gives_empty_string(self) -> None:
        parts = build_query_parts(_make_story(acceptance_criteria=None))
        assert parts["acceptance_criteria"] == ""

    def test_components_joined(self) -> None:
        parts = build_query_parts(_make_story(components=["Chat Widget", "Auth Service"]))
        assert parts["components"] == "Chat Widget, Auth Service"

    def test_components_empty_gives_empty_string(self) -> None:
        parts = build_query_parts(_make_story(components=[]))
        assert parts["components"] == ""

    def test_components_blank_entries_skipped(self) -> None:
        parts = build_query_parts(_make_story(components=["Chat Widget", "  ", ""]))
        assert parts["components"] == "Chat Widget"

    def test_labels_capped_at_max(self) -> None:
        many_labels = [f"label-{i}" for i in range(_MAX_LABELS + 5)]
        parts = build_query_parts(_make_story(labels=many_labels))
        assert len(parts["labels"].split(", ")) == _MAX_LABELS

    def test_labels_empty_gives_empty_string(self) -> None:
        parts = build_query_parts(_make_story(labels=[]))
        assert parts["labels"] == ""

    def test_labels_blank_entries_skipped(self) -> None:
        parts = build_query_parts(_make_story(labels=["security", "  ", ""]))
        assert parts["labels"] == "security"

    def test_returns_all_four_keys(self) -> None:
        parts = build_query_parts(_make_story())
        assert set(parts.keys()) == {"summary", "acceptance_criteria", "components", "labels"}

    def test_ac_newlines_collapsed(self) -> None:
        parts = build_query_parts(_make_story(acceptance_criteria="line1\n\nline2"))
        assert "\n" not in parts["acceptance_criteria"]
        assert "line1 line2" in parts["acceptance_criteria"]


# ── build_query ───────────────────────────────────────────────────────────────

class TestBuildQuery:
    def test_returns_non_empty_string(self) -> None:
        assert build_query(_make_story())

    def test_ends_with_period(self) -> None:
        assert build_query(_make_story()).endswith(".")

    def test_summary_in_query(self) -> None:
        q = build_query(_make_story(summary="Chat Warning"))
        assert "Chat Warning" in q

    def test_ac_in_query(self) -> None:
        q = build_query(_make_story(acceptance_criteria="Disclaimer shows on open."))
        assert "Disclaimer shows on open" in q

    def test_components_prefixed(self) -> None:
        q = build_query(_make_story(components=["Chat Widget"]))
        assert "Components: Chat Widget" in q

    def test_labels_prefixed(self) -> None:
        q = build_query(_make_story(labels=["security"]))
        assert "Tags: security" in q

    def test_no_components_section_when_empty(self) -> None:
        q = build_query(_make_story(components=[]))
        assert "Components:" not in q

    def test_no_labels_section_when_empty(self) -> None:
        q = build_query(_make_story(labels=[]))
        assert "Tags:" not in q

    def test_no_ac_section_when_none(self) -> None:
        q = build_query(_make_story(acceptance_criteria=None, summary="S"))
        # Should just be "S."
        assert q == "S."

    def test_summary_only_when_all_else_empty(self) -> None:
        q = build_query(_make_story(
            summary="Only summary",
            acceptance_criteria=None,
            components=[],
            labels=[],
        ))
        assert q == "Only summary."

    def test_full_query_has_correct_segment_order(self) -> None:
        q = build_query(_make_story(
            summary="Title",
            acceptance_criteria="AC text",
            components=["Widget"],
            labels=["tag1"],
        ))
        # Segments in order: summary . ac . components . labels
        title_pos      = q.index("Title")
        ac_pos         = q.index("AC text")
        component_pos  = q.index("Components:")
        label_pos      = q.index("Tags:")
        assert title_pos < ac_pos < component_pos < label_pos

    def test_no_double_period_at_end(self) -> None:
        q = build_query(_make_story(acceptance_criteria="Text."))
        assert not q.endswith("..")

    def test_query_from_real_aip2_story(self) -> None:
        """Regression test against the actual AIP-2 normalised story."""
        story = StoryContext(
            issue_key="AIP-2",
            summary="Customer Chat Warning Prompt",
            description=(
                "As a customer using the chat,\n"
                "I want to see a clear warning message before I start typing,\n"
                "So that I am reminded to keep my personal financial details private and secure."
            ),
            acceptance_criteria=(
                'A brief disclaimer (e.g., "For your security, please do not share '
                'credit card numbers, bank details, or other financial data in this chat.") '
                "appears when the chat window opens.\n\n"
                "The message is highly visible and placed either just above the text input box "
                "or as the first automated greeting message."
            ),
            labels=[],
            components=[],
        )
        q = build_query(story)
        assert "Customer Chat Warning Prompt" in q
        assert "disclaimer" in q.lower()
        assert "Components:" not in q
        assert "Tags:" not in q
        assert q.endswith(".")
        assert len(q) > 50
