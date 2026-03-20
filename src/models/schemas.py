"""
Internal data models — Schema v1 (locked).

These are the contracts between:
  - the Jira ingestor  →  StoryContext
  - the LLM prompt     →  GeneratedTestCase / GeneratedTestSuite
  - the evaluator      →  GeneratedTestSuite
  - the API response   →  GenerationResponse
  - the retrieval layer→  RetrievalDocument

Do not add or remove fields without bumping the version comment and
updating the prompt template, evaluator, and tests simultaneously.
"""

from __future__ import annotations

from enum import Enum
from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────────────────────

class Priority(str, Enum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class CaseType(str, Enum):
    FUNCTIONAL = "Functional"
    EDGE_CASE = "Edge Case"
    NEGATIVE = "Negative"
    INTEGRATION = "Integration"


# ── Evaluation verdict / failure-category enums ────────────────────────────────

class Verdict(str, Enum):
    """Final decision for one generated test case after all gate rules fire."""
    PASS  = "pass"
    WARN  = "warn"
    BLOCK = "block"


class FailureCategory(str, Enum):
    """Named failure buckets — each maps to a fixed Verdict in DecisionPolicy.

    malformed_but_relevant    – structural fields missing/empty despite the test
                                 appearing topically related.
    grounded_but_duplicate    – substantively the same as a historical test,
                                 grounded in real AC but adds no new coverage.
    relevant_but_unsupported  – references features or AC not present in the story.
    should_refuse_generated   – the story had no actionable AC/description, yet
                                 the LLM still generated a test.
    useful_but_generic        – passes structural checks but steps/expected result
                                 are template-level boilerplate (too vague to run).
    near_duplicate_generated  – almost identical to another test *within this suite*
                                 (intra-suite duplication).
    ac_coverage_incomplete    – one or more acceptance criteria have no test case
                                 whose coverage_tag references them.
    hallucinated_specific_value – test asserts a concrete value (number, quoted
                                 string, error code, time) that appears nowhere in
                                 the story description, AC, or context.
    """
    MALFORMED_BUT_RELEVANT      = "malformed_but_relevant"
    GROUNDED_BUT_DUPLICATE      = "grounded_but_duplicate"
    RELEVANT_BUT_UNSUPPORTED    = "relevant_but_unsupported"
    SHOULD_REFUSE_GENERATED     = "should_refuse_generated"
    USEFUL_BUT_GENERIC          = "useful_but_generic"
    NEAR_DUPLICATE_GENERATED    = "near_duplicate_generated"
    AC_COVERAGE_INCOMPLETE      = "ac_coverage_incomplete"
    HALLUCINATED_SPECIFIC_VALUE = "hallucinated_specific_value"


# ── Per-case gate result ───────────────────────────────────────────────────────

class CaseGateResult(BaseModel):
    """Gate outcome for a single generated test case."""
    title: str
    verdict: Verdict
    # Ordered list of failure categories that fired (empty = clean pass)
    failures: list[FailureCategory] = Field(default_factory=list)
    reasons:  list[str]             = Field(default_factory=list)


# ── Suite-level gate report ────────────────────────────────────────────────────

class SuiteGateReport(BaseModel):
    """Aggregated gate report for a full GeneratedTestSuite."""
    story_key:   str
    suite_verdict: Verdict            # worst verdict across all cases
    case_results:  list[CaseGateResult]
    pass_count:    int
    warn_count:    int
    block_count:   int
    summary:       str                # human-readable one-liner


# ── Input-quality guard (pre-generation rejection layer) ──────────────────────

class InputSignal(str, Enum):
    """Named signals checked by the input guard before generation is attempted.

    missing_acceptance_criteria  – story has no AC field and none in description.
    vague_story                  – description + summary below minimum token/word count.
    weak_context                 – enriched mode requested but context package is empty
                                   or has too few items to be useful.
    conflicting_requirements     – negating phrases detected within the same AC block
                                   (e.g. "must" and "must not" targeting the same noun).
    insufficient_evidence        – combined score of all signals is too low to generate
                                   reliable tests (catch-all / final gate).
    """
    MISSING_AC               = "missing_acceptance_criteria"
    VAGUE_STORY              = "vague_story"
    WEAK_CONTEXT             = "weak_context"
    CONFLICTING_REQUIREMENTS = "conflicting_requirements"
    INSUFFICIENT_EVIDENCE    = "insufficient_evidence"


class InputSignalResult(BaseModel):
    """Result for one input quality signal."""
    signal:  InputSignal
    verdict: Verdict          # PASS / WARN / BLOCK for this signal alone
    detail:  str              # human-readable explanation


class InputGuardReport(BaseModel):
    """Full pre-generation input quality report.

    verdict == BLOCK  → caller MUST NOT call the LLM.
    verdict == WARN   → caller MAY call the LLM but should surface the warning.
    verdict == PASS   → all checks passed; proceed normally.
    """
    issue_key:      str
    verdict:        Verdict
    signal_results: list[InputSignalResult]
    summary:        str   # one-liner for logs / UI


# ── A. Story context (input) ───────────────────────────────────────────────────

class LinkedIssue(BaseModel):
    """B. A Jira issue linked to the story (blocker, sub-task, related, etc.)."""

    key: str = Field(description="Jira issue key, e.g. PROJ-42")
    issue_type: str = Field(description="e.g. Bug, Sub-task, Story, Epic")
    summary: str


class StoryContext(BaseModel):
    """A. Full context extracted from a Jira story, used as LLM input."""

    issue_key: str = Field(description="Jira issue key, e.g. PROJ-123")
    summary: str
    description: str | None = Field(
        default=None,
        description="Full story description / user story body",
    )
    acceptance_criteria: str | None = Field(
        default=None,
        description="Explicit acceptance criteria from the Jira field or description",
    )
    labels: list[str] = Field(
        default_factory=list,
        description="Jira labels attached to the issue",
    )
    components: list[str] = Field(
        default_factory=list,
        description="Jira components the issue belongs to",
    )
    linked_issues: list[LinkedIssue] = Field(
        default_factory=list,
        description="Issues linked to this story (blockers, sub-tasks, related)",
    )


# ── C. Generated test case (output) ───────────────────────────────────────────

class GeneratedTestCase(BaseModel):
    """C. A single test case produced by the LLM for a given story."""

    title: str = Field(description="Short, imperative test title")
    preconditions: list[str] = Field(
        default_factory=list,
        description="Conditions that must be true before the test runs",
    )
    steps: list[str] = Field(
        min_length=1,
        description="Ordered list of test execution steps",
    )
    expected_result: str = Field(
        description="What a passing outcome looks like",
    )
    priority: Priority = Field(default=Priority.MEDIUM)
    test_type: CaseType = Field(default=CaseType.FUNCTIONAL)
    coverage_tag: str = Field(
        default="",
        description="Free-text tag linking this test to an AC or feature area, e.g. 'AC-1' or 'login-flow'",
    )
    source_story: str = Field(
        description="Issue key of the story this test was generated from",
    )


# ── D. Generated test suite (output) ──────────────────────────────────────────

class GeneratedTestSuite(BaseModel):
    """D. The full output for one Jira story — a suite of generated test cases."""

    story_key: str = Field(description="Jira issue key this suite covers")
    tests: list[GeneratedTestCase] = Field(
        min_length=1,
        description="All generated test cases for the story",
    )
    notes: str | None = Field(
        default=None,
        description="Optional LLM commentary — gaps, assumptions, or caveats",
    )


# ── E. Historical context (retrieval layer) ───────────────────────────────────

class ContextItemType(str, Enum):
    """Category of a related issue brought into the context package."""
    BUG = "bug"
    TEST = "test"
    STORY = "story"
    OTHER = "other"


class ContextItem(BaseModel):
    """E. A single normalized related issue — the atom of historical context.

    Deliberately small: only the fields that add signal to generation.
    Raw Jira payloads must never reach the prompt — only ContextItems do.
    """
    key: str = Field(description="Jira issue key, e.g. AIP-10")
    issue_type: str = Field(description="Jira issue type name, e.g. Bug, TestCase, Story")
    category: ContextItemType = Field(description="Semantic category for prompt sectioning")
    summary: str = Field(description="Issue summary — one line max")
    short_text: str | None = Field(
        default=None,
        description="One or two meaningful lines: first sentence of description, AC snippet, or known failure note",
    )
    relevance_hint: str | None = Field(
        default=None,
        description="Why this item was included, e.g. 'linked defect', 'same label: AC-1', 'prior test'",
    )


class ContextPackage(BaseModel):
    """F. The full retrieval-ready bundle passed into the prompt builder.

    Separates concerns clearly so the prompt builder can render each section
    independently and keep the total token budget in check.
    """
    story_key: str
    linked_defects: list[ContextItem] = Field(
        default_factory=list,
        description="Bugs or issues linked directly to the story",
    )
    historical_tests: list[ContextItem] = Field(
        default_factory=list,
        description="Prior test cases from the same feature area",
    )
    related_stories: list[ContextItem] = Field(
        default_factory=list,
        description="Other stories in the same label / component set",
    )
    coverage_hints: list[str] = Field(
        default_factory=list,
        description="Free-text hints inferred from context, e.g. 'AC-1 already covered by AIP-4'",
    )


# ── API request / response ─────────────────────────────────────────────────────

class GenerationRequest(BaseModel):
    """Request body for POST /generate."""

    issue_key: str = Field(description="Jira issue key to generate tests for")


class GenerationResponse(BaseModel):
    """Response body for POST /generate."""

    suite: GeneratedTestSuite
    inline_eval_passed: bool = Field(
        description="True if the suite passed the lightweight inline gate check",
    )


# ── G. Retrieval document (vector/BM25 index atom) ────────────────────────────

class SourceType(str, Enum):
    """The semantic role of the document in the retrieval index.

    story          — a Jira story (user story, new feature, task)
    bug            — a defect / bug report
    historical_test— a previously generated or captured test case
    qa_note        — a freeform QA observation, coverage hint, or tester note
    """
    STORY           = "story"
    BUG             = "bug"
    HISTORICAL_TEST = "historical_test"
    QA_NOTE         = "qa_note"


class RetrievalDocument(BaseModel):
    """G. The single, uniform atom stored in the retrieval index.

    Design goals:
    - One document = one retrievable concept (story, bug, test, note).
    - ``body`` is the only field embeddings / BM25 should score against —
      it is intentionally short (≤ 300 chars) so every document is
      comparably weighted and easy to inspect.
    - Metadata fields (source_type, source_key, tags) are filter-only;
      they must never be concatenated into ``body`` at index time.

    Field contract:
        doc_id       Globally unique within an index; format:
                     ``<source_key>#<source_type>[#<seq>]``
                     e.g. "AIP-2#story", "AIP-10#bug", "AIP-2#historical_test#0"
        source_type  Semantic role — drives prompt sectioning and eval filtering.
        source_key   Originating Jira key (story being indexed or related issue key).
        title        One short imperative/noun phrase (≤ 80 chars).
                     Must be human-readable and inspection-friendly.
        body         Single compact paragraph (≤ 300 chars).
                     Concatenates only the most signal-rich fields:
                       story   → description first sentence + AC first sentence
                       bug     → summary + short_text
                       test    → title + expected_result
                       qa_note → the note itself
        tags         Optional free metadata for post-retrieval filtering.
    """

    doc_id: str = Field(
        description="Unique document identifier: <source_key>#<source_type>[#<seq>]",
    )
    source_type: SourceType = Field(
        description="Semantic role of this document in the retrieval index",
    )
    source_key: str = Field(
        description="Originating Jira issue key, e.g. AIP-2",
    )
    title: str = Field(
        max_length=80,
        description="Short human-readable label; used for logging and inspection",
    )
    body: str = Field(
        max_length=300,
        description="Single compact text block that embeddings / BM25 score against",
    )
    components: list[str] = Field(
        default_factory=list,
        description="Jira components — metadata filter, not indexed in body",
    )
    labels: list[str] = Field(
        default_factory=list,
        description="Jira labels — metadata filter, not indexed in body",
    )
    feature_area: str | None = Field(
        default=None,
        description="Free-text feature area tag, e.g. 'chat-widget', 'auth-flow'",
    )


# ── H. AC coverage report (new quality check) ─────────────────────────────────

class AcCoverageItem(BaseModel):
    """Coverage result for one extracted acceptance criterion."""
    ac_label: str = Field(description="Extracted AC identifier or first words, e.g. 'AC-1'")
    ac_text: str = Field(description="The raw AC text or first sentence")
    covered: bool = Field(description="True if at least one test has a matching coverage_tag")
    covering_tests: list[str] = Field(
        default_factory=list,
        description="Titles of tests whose coverage_tag matched this AC",
    )


class AcCoverageReport(BaseModel):
    """Suite-level AC coverage completeness report."""
    story_key: str
    total_ac: int = 0
    covered_ac: int = 0
    uncovered_ac: int = 0
    coverage_ratio: float = 0.0          # covered_ac / total_ac, or 1.0 if no AC found
    items: list[AcCoverageItem] = Field(default_factory=list)
    phantom_tags: list[dict] = Field(
        default_factory=list,
        description="Tests whose coverage_tag references an AC label not found in the story",
    )
    verdict: Verdict = Verdict.PASS
    summary: str = ""


# ── I. Per-test semantic relevancy (new quality check) ────────────────────────

class PerTestRelevancyResult(BaseModel):
    """LLM-judge relevancy score for one individual test case."""
    test_index: int
    title: str
    score: float
    passed: bool
    reason: str = ""


class PerTestRelevancyReport(BaseModel):
    """Suite-level per-test relevancy report."""
    story_key: str
    threshold: float
    results: list[PerTestRelevancyResult] = Field(default_factory=list)
    pass_count: int = 0
    fail_count: int = 0
    avg_score: float = 0.0
    verdict: Verdict = Verdict.PASS
    summary: str = ""


# ── J. Hallucinated specific values (new quality check) ───────────────────────

class HallucinationFlag(BaseModel):
    """One detected hallucinated claim in a test case."""
    test_index: int
    title: str
    hallucinated_values: list[str] = Field(
        description="Numeric literals or quoted strings not found in story/context",
    )
    reason: str = ""


class HallucinationReport(BaseModel):
    """Suite-level hallucination report."""
    story_key: str
    flags: list[HallucinationFlag] = Field(default_factory=list)
    clean_count: int = 0
    flagged_count: int = 0
    verdict: Verdict = Verdict.PASS
    summary: str = ""


# ── K. Latency metrics ────────────────────────────────────────────────────────

class LatencyMetrics(BaseModel):
    """Timing data captured during a generation run."""
    story_key: str
    generation_seconds: float | None = None
    evaluation_seconds: float | None = None
    total_seconds: float | None = None
    recorded_at: str = ""               # ISO-8601 timestamp
