"""Builds the prompt sent to Gemini for test-case generation.

Keeping the prompt in its own module means:
- it can be unit-tested without an API key
- it can be versioned independently of the generator
- the LLM never sees raw Jira structure
"""

from __future__ import annotations

from src.models.schemas import CaseType, ContextItemType, ContextPackage, Priority, StoryContext


# ── Exact enum values Gemini must use ─────────────────────────────────────────

_PRIORITY_VALUES = " | ".join(p.value for p in Priority)
_TYPE_VALUES = " | ".join(t.value for t in CaseType)


# ── JSON schema shown to Gemini — uses real enum values, not placeholders ─────

def _schema_block(issue_key: str) -> str:
    return f"""{{
  "story_key": "{issue_key}",
  "tests": [
    {{
      "title": "<imperative sentence describing what is being tested>",
      "preconditions": ["<state that must be true before this test runs>"],
      "steps": [
        "<step 1: concrete user or system action>",
        "<step 2: ...>"
      ],
      "expected_result": "<observable outcome that defines a pass>",
      "priority": "{_PRIORITY_VALUES}",
      "test_type": "{_TYPE_VALUES}",
      "coverage_tag": "<AC-1 | AC-2 | or short feature area from the story>",
      "source_story": "{issue_key}"
    }}
  ],
  "notes": "<one sentence: gaps or assumptions only — omit if none>"
}}"""


# ── Hard rules block ──────────────────────────────────────────────────────────

_HARD_RULES_TEMPLATE = """\
HARD RULES — violating any of these makes the output unusable:
  R1. Return ONLY the JSON object. No markdown. No code fences. No prose before or after.
  R2. Do not invent any requirement, field, or behaviour not stated in the story above.
  R3. Use ONLY these exact priority values (case-sensitive): {priorities}
  R4. Use ONLY these exact test_type values (case-sensitive): {types}
  R5. Every test must have source_story = "{issue_key}".
  R6. Every step must be a concrete, executable action — not "verify it works".
  R7. The output must be complete and valid JSON — do not truncate mid-string."""


# ── Context block renderer ────────────────────────────────────────────────────

def _context_block(package: ContextPackage) -> str:
    """Render a ContextPackage into a compact, prompt-ready text block.

    Design rules:
    - Each item is one line: [TYPE] KEY: summary  (short_text if present)
    - Sections are only included if they have content (no empty headers)
    - Coverage hints are listed last — they directly guide the TASK section
    - Total length is bounded: max 3 items per section keeps token budget low
    """
    lines: list[str] = []

    def _item_line(item) -> str:
        text = f"  [{item.issue_type}] {item.key}: {item.summary}"
        if item.short_text:
            text += f" — {item.short_text[:80]}"
        return text

    if package.linked_defects:
        lines.append("Known Defects (linked to this story):")
        for item in package.linked_defects[:3]:
            lines.append(_item_line(item))

    if package.historical_tests:
        lines.append("Prior Test Cases (same feature area):")
        for item in package.historical_tests[:3]:
            lines.append(_item_line(item))

    if package.related_stories:
        lines.append("Related Stories:")
        for item in package.related_stories[:3]:
            lines.append(_item_line(item))

    if package.coverage_hints:
        lines.append("Coverage Hints (use these to avoid duplication and target gaps):")
        for hint in package.coverage_hints:
            lines.append(f"  → {hint}")

    return "\n".join(lines)


# ── Public API ────────────────────────────────────────────────────────────────

def build_prompt(
    story: StoryContext,
    max_tests: int = 5,
    context: ContextPackage | None = None,
) -> str:
    """Return the full prompt string to send to Gemini.

    Design principles:
    - Story fields only — Gemini never sees raw Jira structure
    - Exact enum values are injected from Python enums (single source of truth)
    - Hard rules are numbered so failures can be traced to a specific rule
    - Schema uses the real issue_key so Gemini has no excuse to get it wrong
    - Optional ContextPackage is rendered as a compact HISTORICAL CONTEXT section
    """
    linked = ""
    if story.linked_issues:
        items = "\n".join(
            f"  - [{li.issue_type}] {li.key}: {li.summary}"
            for li in story.linked_issues
        )
        linked = f"\nLinked Issues:\n{items}"

    labels = f"\nLabels: {', '.join(story.labels)}" if story.labels else ""
    components = f"\nComponents: {', '.join(story.components)}" if story.components else ""

    hard_rules = _HARD_RULES_TEMPLATE.format(
        priorities=_PRIORITY_VALUES,
        types=_TYPE_VALUES,
        issue_key=story.issue_key,
    )

    # Build the optional historical context section
    context_section = ""
    if context and (
        context.linked_defects
        or context.historical_tests
        or context.related_stories
        or context.coverage_hints
    ):
        context_section = f"""
------------------------------------------------------------
HISTORICAL CONTEXT (read-only — do not copy, only use for awareness)
------------------------------------------------------------
{_context_block(context)}
"""

    return f"""You are a senior QA engineer generating test cases from a Jira story.
Your output will be parsed by a machine. Any text outside the JSON object will cause a failure.

------------------------------------------------------------
STORY
------------------------------------------------------------
Issue Key : {story.issue_key}
Summary   : {story.summary}

Description:
{story.description or "(not provided)"}

Acceptance Criteria:
{story.acceptance_criteria or "(not provided)"}
{labels}{components}{linked}
{context_section}
------------------------------------------------------------
TASK
------------------------------------------------------------
Generate between 3 and {max_tests} test cases for the story above.

Requirements:
  - Include AT LEAST ONE test with test_type = "Negative"
  - Every test must be grounded in the story facts above — no invented behaviour
  - coverage_tag must reference the part of the story each test covers (e.g. AC-1, AC-2)
  - Steps must describe what a tester actually does, not what the system should do
  - Use the Historical Context above to avoid duplicating known tests and to target gaps

------------------------------------------------------------
{hard_rules}

------------------------------------------------------------
OUTPUT SCHEMA (fill in all fields exactly as shown)
------------------------------------------------------------
{_schema_block(story.issue_key)}"""
