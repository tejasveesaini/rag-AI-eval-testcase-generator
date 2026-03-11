"""
Internal data models — Schema v1 (locked).

These are the contracts between:
  - the Jira ingestor  →  StoryContext
  - the LLM prompt     →  GeneratedTestCase / GeneratedTestSuite
  - the evaluator      →  GeneratedTestSuite
  - the API response   →  GenerationResponse

Do not add or remove fields without bumping the version comment and
updating the prompt template, evaluator, and tests simultaneously.
"""

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
