"""Converts internal domain objects into uniform RetrievalDocuments.

One RetrievalDocument = one retrievable concept.
Every factory here follows the same contract:

  doc_id       → "<source_key>#<source_type>[#<seq>]"
  title        → ≤ 80 chars, human-readable, inspection-friendly
  body         → ≤ 300 chars, only the highest-signal text fields joined
  components / labels / feature_area → metadata only (never in body)

Factory map:
  from_story_context(story)            → SourceType.STORY
  from_context_item(item, seq)         → SourceType.BUG | STORY | HISTORICAL_TEST
  from_generated_test_case(tc, seq)    → SourceType.HISTORICAL_TEST
  from_qa_note(source_key, note, seq)  → SourceType.QA_NOTE

Public API:
  build_retrieval_index(story, package, suite) → list[RetrievalDocument]
"""

from __future__ import annotations

import textwrap
from typing import Sequence

from src.models.schemas import (
    ContextItem,
    ContextItemType,
    ContextPackage,
    GeneratedTestCase,
    GeneratedTestSuite,
    RetrievalDocument,
    SourceType,
    StoryContext,
)

# Maximum character lengths for body composition
_TITLE_MAX  = 80
_BODY_MAX   = 300
_SNIP_MAX   = 150   # max chars taken from any single field before joining


# ── Helpers ───────────────────────────────────────────────────────────────────

def _truncate(text: str | None, limit: int) -> str:
    """Return text truncated to limit chars, stripped, or empty string."""
    if not text:
        return ""
    return text.strip()[:limit]


def _compact_body(*parts: str | None, sep: str = " | ") -> str:
    """Join non-empty parts with sep and hard-cap at _BODY_MAX chars.

    The separator is a pipe ( | ) so the body reads clearly in logs:
      "Customer Chat Warning Prompt | As a customer using the chat..."
    """
    joined = sep.join(p.strip() for p in parts if p and p.strip())
    return joined[:_BODY_MAX]


def _source_type_from_category(category: ContextItemType) -> SourceType:
    """Map a ContextItemType to the matching SourceType for the index."""
    return {
        ContextItemType.BUG:   SourceType.BUG,
        ContextItemType.TEST:  SourceType.HISTORICAL_TEST,
        ContextItemType.STORY: SourceType.STORY,
        ContextItemType.OTHER: SourceType.HISTORICAL_TEST,   # default to test slot
    }[category]


# ── Factory functions ─────────────────────────────────────────────────────────

def from_story_context(story: StoryContext) -> RetrievalDocument:
    """Build one STORY document from a normalized StoryContext.

    Body composition (highest → lowest signal):
      summary  +  description first sentence  +  AC first sentence

    All three are present in most well-written stories; any missing piece
    is simply omitted so the body stays tight.
    """
    # First non-empty line of description (skip pure-whitespace lines)
    desc_snippet: str | None = None
    if story.description:
        for line in story.description.splitlines():
            line = line.strip()
            if len(line) >= 15:
                desc_snippet = line[:_SNIP_MAX]
                break

    # First non-empty line of acceptance criteria
    ac_snippet: str | None = None
    if story.acceptance_criteria:
        for line in story.acceptance_criteria.splitlines():
            line = line.strip()
            if len(line) >= 15:
                ac_snippet = line[:_SNIP_MAX]
                break

    body = _compact_body(story.summary, desc_snippet, ac_snippet)

    return RetrievalDocument(
        doc_id=f"{story.issue_key}#story",
        source_type=SourceType.STORY,
        source_key=story.issue_key,
        title=_truncate(story.summary, _TITLE_MAX),
        body=body,
        components=list(story.components),
        labels=list(story.labels),
        feature_area=None,   # caller may set via model_copy() after the fact
    )


def from_context_item(item: ContextItem, seq: int = 0) -> RetrievalDocument:
    """Build a BUG, STORY, or HISTORICAL_TEST document from a ContextItem.

    Body composition:
      summary  +  short_text (if available)

    The relevance_hint is intentionally NOT included in the body — it is
    retrieval metadata, not retrieval signal.
    """
    source_type = _source_type_from_category(item.category)
    doc_id      = f"{item.key}#{source_type.value}"
    if seq:
        doc_id += f"#{seq}"

    body = _compact_body(item.summary, item.short_text)

    return RetrievalDocument(
        doc_id=doc_id,
        source_type=source_type,
        source_key=item.key,
        title=_truncate(item.summary, _TITLE_MAX),
        body=body,
        components=[],
        labels=[],
        feature_area=None,
    )


def from_generated_test_case(tc: GeneratedTestCase, seq: int) -> RetrievalDocument:
    """Build a HISTORICAL_TEST document from a GeneratedTestCase.

    Body composition:
      title  +  expected_result

    Steps are excluded from the body: they are procedural detail, not
    the conceptual signal we want retrieved ("what does this test check?").
    The coverage_tag is stored as feature_area for post-retrieval filtering.
    """
    doc_id = f"{tc.source_story}#historical_test#{seq}"

    body = _compact_body(tc.title, tc.expected_result)

    return RetrievalDocument(
        doc_id=doc_id,
        source_type=SourceType.HISTORICAL_TEST,
        source_key=tc.source_story,
        title=_truncate(tc.title, _TITLE_MAX),
        body=body,
        components=[],
        labels=[],
        feature_area=tc.coverage_tag or None,
    )


def from_qa_note(source_key: str, note: str, seq: int = 0) -> RetrievalDocument:
    """Build a QA_NOTE document from a free-text coverage hint or tester note.

    Body IS the note itself — it is already compact prose generated from
    the packager's heuristics, so no further transformation is needed.
    """
    doc_id = f"{source_key}#qa_note#{seq}"

    body = _truncate(note, _BODY_MAX)

    # Title: first sentence up to 80 chars (strip trailing punctuation)
    title = body.split(".")[0].rstrip(".,;: ")[:_TITLE_MAX]

    return RetrievalDocument(
        doc_id=doc_id,
        source_type=SourceType.QA_NOTE,
        source_key=source_key,
        title=title,
        body=body,
        components=[],
        labels=[],
        feature_area=None,
    )


# ── Index builder ─────────────────────────────────────────────────────────────

def build_retrieval_index(
    story: StoryContext,
    package: ContextPackage | None = None,
    suite: GeneratedTestSuite | None = None,
) -> list[RetrievalDocument]:
    """Produce the complete set of RetrievalDocuments for one story.

    Ordering (stable — important for deterministic doc_id sequences):
      1. The story itself                  → 1 document  (STORY)
      2. Linked defects from package       → N documents (BUG)
      3. Historical tests from package     → N documents (HISTORICAL_TEST)
      4. Related stories from package      → N documents (STORY)
      5. Generated test cases from suite   → N documents (HISTORICAL_TEST)
      6. Coverage hints from package       → N documents (QA_NOTE)

    Deduplication: doc_ids must be unique within the returned list.
    If the same Jira key appears in multiple sections (e.g. a linked defect
    that was also returned by JQL), only the first occurrence is kept.

    Args:
        story:   Normalized StoryContext — always present.
        package: ContextPackage from the context pipeline — optional.
                 When None, only the story document and suite tests are indexed.
        suite:   GeneratedTestSuite — optional.
                 When None, no generated tests are added to the index.

    Returns:
        A list of RetrievalDocuments with unique doc_ids, ready to upsert
        into any vector store or BM25 index.
    """
    docs: list[RetrievalDocument] = []
    seen_ids: set[str] = set()

    def _add(doc: RetrievalDocument) -> None:
        if doc.doc_id not in seen_ids:
            docs.append(doc)
            seen_ids.add(doc.doc_id)

    # 1. Story
    _add(from_story_context(story))

    if package:
        # 2. Linked defects
        for item in package.linked_defects:
            _add(from_context_item(item))

        # 3. Historical tests
        for seq, item in enumerate(package.historical_tests):
            _add(from_context_item(item, seq=seq))

        # 4. Related stories
        for item in package.related_stories:
            _add(from_context_item(item))

    # 5. Generated test cases (from suite)
    if suite:
        for seq, tc in enumerate(suite.tests):
            _add(from_generated_test_case(tc, seq=seq))

    if package:
        # 6. Coverage hints (QA notes)
        for seq, hint in enumerate(package.coverage_hints):
            _add(from_qa_note(story.issue_key, hint, seq=seq))

    return docs
