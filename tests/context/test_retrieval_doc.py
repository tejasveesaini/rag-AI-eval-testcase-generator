"""Tests for src/context/retrieval_doc.py — pure unit tests, no I/O, no HTTP.

Coverage map:
  from_story_context        → body composition, doc_id format, field mapping
  from_context_item         → all three category branches (bug, test, story)
  from_generated_test_case  → body = title + expected_result, feature_area
  from_qa_note              → body IS the note, title derived
  build_retrieval_index     → ordering, deduplication, optional inputs
  RetrievalDocument         → body ≤ 300 chars, title ≤ 80 chars invariants
"""

from __future__ import annotations

import pytest

from src.context.retrieval_doc import (
    build_retrieval_index,
    from_context_item,
    from_generated_test_case,
    from_qa_note,
    from_story_context,
)
from src.models.schemas import (
    CaseType,
    ContextItem,
    ContextItemType,
    ContextPackage,
    GeneratedTestCase,
    GeneratedTestSuite,
    Priority,
    RetrievalDocument,
    SourceType,
    StoryContext,
)


# ── Shared fixtures ───────────────────────────────────────────────────────────

@pytest.fixture()
def story() -> StoryContext:
    return StoryContext(
        issue_key="AIP-2",
        summary="Customer Chat Warning Prompt",
        description=(
            "As a customer using the chat,\n"
            "I want to see a clear warning message before I start typing,\n"
            "So that I am reminded to keep my personal financial details private."
        ),
        acceptance_criteria=(
            "A brief disclaimer appears when the chat window opens.\n"
            "The message is highly visible above the input box."
        ),
        labels=["chat", "security"],
        components=["Chat Widget"],
    )


@pytest.fixture()
def bug_item() -> ContextItem:
    return ContextItem(
        key="AIP-10",
        issue_type="Bug",
        category=ContextItemType.BUG,
        summary="Chat input allows forbidden characters",
        short_text="Click inside the chat input.",
        relevance_hint="linked issue",
    )


@pytest.fixture()
def test_item() -> ContextItem:
    return ContextItem(
        key="AIP-5",
        issue_type="TestCase",
        category=ContextItemType.TEST,
        summary="Verify disclaimer appears on chat open",
        short_text="Expected: disclaimer visible immediately.",
        relevance_hint="same label/component",
    )


@pytest.fixture()
def story_item() -> ContextItem:
    return ContextItem(
        key="AIP-3",
        issue_type="Story",
        category=ContextItemType.STORY,
        summary="Chat session timeout warning",
        short_text=None,
        relevance_hint="same component",
    )


@pytest.fixture()
def test_case() -> GeneratedTestCase:
    return GeneratedTestCase(
        title="Verify security disclaimer appears on chat initialization",
        preconditions=["User is on a page with the chat widget enabled"],
        steps=["Click on the chat icon to open the chat window."],
        expected_result="A security disclaimer is visible immediately upon opening.",
        priority=Priority.HIGH,
        test_type=CaseType.FUNCTIONAL,
        coverage_tag="AC-1",
        source_story="AIP-2",
    )


@pytest.fixture()
def package(bug_item: ContextItem, test_item: ContextItem) -> ContextPackage:
    return ContextPackage(
        story_key="AIP-2",
        linked_defects=[bug_item],
        historical_tests=[test_item],
        related_stories=[],
        coverage_hints=["Known linked defects for this story: AIP-10 — consider regression tests"],
    )


@pytest.fixture()
def suite(test_case: GeneratedTestCase) -> GeneratedTestSuite:
    return GeneratedTestSuite(story_key="AIP-2", tests=[test_case])


# ── from_story_context ────────────────────────────────────────────────────────

class TestFromStoryContext:
    def test_doc_id_format(self, story: StoryContext) -> None:
        doc = from_story_context(story)
        assert doc.doc_id == "AIP-2#story"

    def test_source_type_is_story(self, story: StoryContext) -> None:
        doc = from_story_context(story)
        assert doc.source_type == SourceType.STORY

    def test_source_key_matches_issue_key(self, story: StoryContext) -> None:
        doc = from_story_context(story)
        assert doc.source_key == "AIP-2"

    def test_title_is_summary(self, story: StoryContext) -> None:
        doc = from_story_context(story)
        assert doc.title == "Customer Chat Warning Prompt"

    def test_body_contains_summary(self, story: StoryContext) -> None:
        doc = from_story_context(story)
        assert "Customer Chat Warning Prompt" in doc.body

    def test_body_contains_description_snippet(self, story: StoryContext) -> None:
        doc = from_story_context(story)
        # The description's first meaningful line (≥15 chars) should be in the body.
        # Fixture: first line is "As a customer using the chat," — 29 chars.
        assert "customer using the chat" in doc.body

    def test_body_contains_ac_snippet(self, story: StoryContext) -> None:
        doc = from_story_context(story)
        assert "disclaimer" in doc.body

    def test_body_max_300_chars(self, story: StoryContext) -> None:
        doc = from_story_context(story)
        assert len(doc.body) <= 300

    def test_labels_forwarded(self, story: StoryContext) -> None:
        doc = from_story_context(story)
        assert "chat" in doc.labels
        assert "security" in doc.labels

    def test_components_forwarded(self, story: StoryContext) -> None:
        doc = from_story_context(story)
        assert "Chat Widget" in doc.components

    def test_story_with_no_description(self) -> None:
        story = StoryContext(issue_key="X-1", summary="Bare story", description=None, acceptance_criteria=None)
        doc = from_story_context(story)
        assert doc.body == "Bare story"
        assert doc.title == "Bare story"

    def test_title_max_80_chars(self) -> None:
        long_summary = "A" * 100
        story = StoryContext(issue_key="X-1", summary=long_summary)
        doc = from_story_context(story)
        assert len(doc.title) <= 80


# ── from_context_item — BUG ───────────────────────────────────────────────────

class TestFromContextItemBug:
    def test_source_type_is_bug(self, bug_item: ContextItem) -> None:
        doc = from_context_item(bug_item)
        assert doc.source_type == SourceType.BUG

    def test_doc_id_format(self, bug_item: ContextItem) -> None:
        doc = from_context_item(bug_item)
        assert doc.doc_id == "AIP-10#bug"

    def test_doc_id_with_seq(self, bug_item: ContextItem) -> None:
        doc = from_context_item(bug_item, seq=2)
        assert doc.doc_id == "AIP-10#bug#2"

    def test_body_combines_summary_and_short_text(self, bug_item: ContextItem) -> None:
        doc = from_context_item(bug_item)
        assert "Chat input allows forbidden characters" in doc.body
        assert "Click inside the chat input." in doc.body

    def test_relevance_hint_not_in_body(self, bug_item: ContextItem) -> None:
        doc = from_context_item(bug_item)
        assert "linked issue" not in doc.body

    def test_body_max_300_chars(self, bug_item: ContextItem) -> None:
        doc = from_context_item(bug_item)
        assert len(doc.body) <= 300


# ── from_context_item — HISTORICAL_TEST ──────────────────────────────────────

class TestFromContextItemTest:
    def test_source_type_is_historical_test(self, test_item: ContextItem) -> None:
        doc = from_context_item(test_item)
        assert doc.source_type == SourceType.HISTORICAL_TEST

    def test_doc_id_format(self, test_item: ContextItem) -> None:
        doc = from_context_item(test_item)
        assert doc.doc_id == "AIP-5#historical_test"

    def test_body_contains_summary(self, test_item: ContextItem) -> None:
        doc = from_context_item(test_item)
        assert "disclaimer" in doc.body


# ── from_context_item — STORY ────────────────────────────────────────────────

class TestFromContextItemStory:
    def test_source_type_is_story(self, story_item: ContextItem) -> None:
        doc = from_context_item(story_item)
        assert doc.source_type == SourceType.STORY

    def test_body_when_short_text_is_none(self, story_item: ContextItem) -> None:
        doc = from_context_item(story_item)
        assert doc.body == "Chat session timeout warning"

    def test_doc_id_no_seq_suffix_when_seq_is_zero(self, story_item: ContextItem) -> None:
        doc = from_context_item(story_item, seq=0)
        assert "#0" not in doc.doc_id


# ── from_context_item — OTHER category ──────────────────────────────────────

def test_other_category_maps_to_historical_test() -> None:
    item = ContextItem(
        key="AIP-99",
        issue_type="Sub-task",
        category=ContextItemType.OTHER,
        summary="Some sub-task",
        short_text=None,
        relevance_hint=None,
    )
    doc = from_context_item(item)
    assert doc.source_type == SourceType.HISTORICAL_TEST


# ── from_generated_test_case ─────────────────────────────────────────────────

class TestFromGeneratedTestCase:
    def test_source_type(self, test_case: GeneratedTestCase) -> None:
        doc = from_generated_test_case(test_case, seq=0)
        assert doc.source_type == SourceType.HISTORICAL_TEST

    def test_doc_id_format(self, test_case: GeneratedTestCase) -> None:
        doc = from_generated_test_case(test_case, seq=0)
        assert doc.doc_id == "AIP-2#historical_test#0"

    def test_doc_id_seq_increments(self, test_case: GeneratedTestCase) -> None:
        doc = from_generated_test_case(test_case, seq=3)
        assert doc.doc_id == "AIP-2#historical_test#3"

    def test_body_contains_title(self, test_case: GeneratedTestCase) -> None:
        doc = from_generated_test_case(test_case, seq=0)
        assert "security disclaimer" in doc.body

    def test_body_contains_expected_result(self, test_case: GeneratedTestCase) -> None:
        doc = from_generated_test_case(test_case, seq=0)
        assert "visible immediately" in doc.body

    def test_steps_not_in_body(self, test_case: GeneratedTestCase) -> None:
        doc = from_generated_test_case(test_case, seq=0)
        # Steps are procedural detail — must not appear in the body
        assert "Click on the chat icon" not in doc.body

    def test_coverage_tag_becomes_feature_area(self, test_case: GeneratedTestCase) -> None:
        doc = from_generated_test_case(test_case, seq=0)
        assert doc.feature_area == "AC-1"

    def test_body_max_300_chars(self, test_case: GeneratedTestCase) -> None:
        doc = from_generated_test_case(test_case, seq=0)
        assert len(doc.body) <= 300

    def test_source_key_is_source_story(self, test_case: GeneratedTestCase) -> None:
        doc = from_generated_test_case(test_case, seq=0)
        assert doc.source_key == "AIP-2"


# ── from_qa_note ─────────────────────────────────────────────────────────────

class TestFromQaNote:
    def test_source_type(self) -> None:
        doc = from_qa_note("AIP-2", "Known linked defects: AIP-10 — consider regression tests", seq=0)
        assert doc.source_type == SourceType.QA_NOTE

    def test_doc_id_format(self) -> None:
        doc = from_qa_note("AIP-2", "Some note", seq=0)
        assert doc.doc_id == "AIP-2#qa_note#0"

    def test_doc_id_seq_varies(self) -> None:
        doc1 = from_qa_note("AIP-2", "Note one", seq=0)
        doc2 = from_qa_note("AIP-2", "Note two", seq=1)
        assert doc1.doc_id != doc2.doc_id

    def test_body_is_the_note(self) -> None:
        note = "Known linked defects: AIP-10 — consider regression tests"
        doc  = from_qa_note("AIP-2", note, seq=0)
        assert doc.body == note

    def test_body_max_300_chars_on_long_note(self) -> None:
        long_note = "X" * 500
        doc = from_qa_note("AIP-2", long_note, seq=0)
        assert len(doc.body) <= 300

    def test_title_derived_from_note(self) -> None:
        note = "Known linked defects: AIP-10. Extra detail here."
        doc  = from_qa_note("AIP-2", note, seq=0)
        # Title should be the first sentence (before the full stop)
        assert "AIP-10" in doc.title
        assert len(doc.title) <= 80

    def test_source_key(self) -> None:
        doc = from_qa_note("AIP-2", "A note", seq=0)
        assert doc.source_key == "AIP-2"


# ── build_retrieval_index ─────────────────────────────────────────────────────

class TestBuildRetrievalIndex:
    def test_story_only_produces_one_doc(self, story: StoryContext) -> None:
        docs = build_retrieval_index(story)
        assert len(docs) == 1
        assert docs[0].source_type == SourceType.STORY

    def test_first_doc_is_always_the_story(
        self,
        story: StoryContext,
        package: ContextPackage,
        suite: GeneratedTestSuite,
    ) -> None:
        docs = build_retrieval_index(story, package=package, suite=suite)
        assert docs[0].source_type == SourceType.STORY
        assert docs[0].source_key == "AIP-2"

    def test_all_doc_ids_are_unique(
        self,
        story: StoryContext,
        package: ContextPackage,
        suite: GeneratedTestSuite,
    ) -> None:
        docs = build_retrieval_index(story, package=package, suite=suite)
        ids = [d.doc_id for d in docs]
        assert len(ids) == len(set(ids)), f"Duplicate doc_ids found: {ids}"

    def test_bug_documents_present(
        self, story: StoryContext, package: ContextPackage
    ) -> None:
        docs = build_retrieval_index(story, package=package)
        types = {d.source_type for d in docs}
        assert SourceType.BUG in types

    def test_historical_test_from_package_present(
        self, story: StoryContext, package: ContextPackage
    ) -> None:
        docs = build_retrieval_index(story, package=package)
        types = {d.source_type for d in docs}
        assert SourceType.HISTORICAL_TEST in types

    def test_qa_note_from_coverage_hints(
        self, story: StoryContext, package: ContextPackage
    ) -> None:
        docs = build_retrieval_index(story, package=package)
        types = {d.source_type for d in docs}
        assert SourceType.QA_NOTE in types

    def test_generated_tests_indexed_as_historical_test(
        self,
        story: StoryContext,
        suite: GeneratedTestSuite,
    ) -> None:
        docs = build_retrieval_index(story, suite=suite)
        historical = [d for d in docs if d.source_type == SourceType.HISTORICAL_TEST]
        assert len(historical) == len(suite.tests)

    def test_no_package_no_qa_notes(
        self, story: StoryContext, suite: GeneratedTestSuite
    ) -> None:
        docs = build_retrieval_index(story, suite=suite)
        qa_notes = [d for d in docs if d.source_type == SourceType.QA_NOTE]
        assert qa_notes == []

    def test_deduplication_same_key_kept_once(
        self,
        story: StoryContext,
    ) -> None:
        """If the same key appears twice in linked_defects (unusual but possible),
        only the first occurrence should appear in the index."""
        dupe_item = ContextItem(
            key="AIP-10",
            issue_type="Bug",
            category=ContextItemType.BUG,
            summary="Duplicate bug",
            short_text=None,
            relevance_hint="linked issue",
        )
        package = ContextPackage(
            story_key="AIP-2",
            linked_defects=[dupe_item, dupe_item],
            historical_tests=[],
            related_stories=[],
            coverage_hints=[],
        )
        docs = build_retrieval_index(story, package=package)
        ids = [d.doc_id for d in docs]
        assert ids.count("AIP-10#bug") == 1

    def test_total_count_with_all_inputs(
        self,
        story: StoryContext,
        package: ContextPackage,
        suite: GeneratedTestSuite,
    ) -> None:
        """
        Expected breakdown:
          1  story
          1  bug   (AIP-10 linked defect)
          1  historical_test from package (AIP-5)
          1  historical_test from suite   (test_case, seq=0)
          1  qa_note from coverage_hints
        = 5 total
        """
        docs = build_retrieval_index(story, package=package, suite=suite)
        assert len(docs) == 5

    def test_all_bodies_are_non_empty(
        self,
        story: StoryContext,
        package: ContextPackage,
        suite: GeneratedTestSuite,
    ) -> None:
        docs = build_retrieval_index(story, package=package, suite=suite)
        for doc in docs:
            assert doc.body.strip(), f"Empty body on doc {doc.doc_id}"

    def test_all_bodies_within_300_chars(
        self,
        story: StoryContext,
        package: ContextPackage,
        suite: GeneratedTestSuite,
    ) -> None:
        docs = build_retrieval_index(story, package=package, suite=suite)
        for doc in docs:
            assert len(doc.body) <= 300, (
                f"doc {doc.doc_id} body is {len(doc.body)} chars (max 300)"
            )

    def test_all_titles_within_80_chars(
        self,
        story: StoryContext,
        package: ContextPackage,
        suite: GeneratedTestSuite,
    ) -> None:
        docs = build_retrieval_index(story, package=package, suite=suite)
        for doc in docs:
            assert len(doc.title) <= 80, (
                f"doc {doc.doc_id} title is {len(doc.title)} chars (max 80)"
            )

    def test_returns_list_of_retrieval_documents(
        self, story: StoryContext
    ) -> None:
        docs = build_retrieval_index(story)
        assert all(isinstance(d, RetrievalDocument) for d in docs)
