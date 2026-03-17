"""Builds the prompt sent to Gemini for test-case generation.

Two generation modes:
  baseline  — story only (no context package)
  enriched  — story + ContextPackage (linked defects, prior tests, hints)

Both modes share the same hard rules and JSON schema.
The authority hierarchy is enforced in the CONTEXT RULES section:
  current story > historical context (story always wins).
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
  R2. Do not invent any requirement, field, or behaviour not stated in the STORY section above.
  R3. Use ONLY these exact priority values (case-sensitive): {priorities}
  R4. Use ONLY these exact test_type values (case-sensitive): {types}
  R5. Every test must have source_story = "{issue_key}".
  R6. Every step must be a concrete, executable action — not "verify it works".
  R7. The output must be complete and valid JSON — do not truncate mid-string.
  R8. expected_result must state a directly observable outcome. Never write phrases like
      "as specified", "per requirements", "according to spec", "as required", or
      "correctly positioned according" — state the actual observable behaviour instead."""


# ── Context authority rules (only rendered when context is present) ────────────

_CONTEXT_AUTHORITY_RULES = """\
CONTEXT RULES — how to use the Historical Context section:
  C1. The current story (above) is the SOLE source of truth for requirements.
      Historical context is supportive only — it does NOT add new requirements.
  C2. Do NOT copy existing test cases. Use them only to identify coverage gaps.
  C3. If a known defect is listed, you MAY write one regression or negative test
      for that failure area — BUT ONLY if the failure area is already in the
      current story's scope. Do NOT treat the defect's platform, browser, or
      character set as a new story requirement.
  C4. PROHIBITED: any step or expected_result that references a platform name
      (e.g. "Safari", "Chrome"), input type (e.g. "forbidden characters"), or
      system detail that appears ONLY in the Historical Context and NOT in the
      story description or acceptance criteria.
  C5. Do NOT infer requirements from historical context that are absent from the story.
  C6. If historical context conflicts with the current story, follow the current story."""


# ── Context block renderer ────────────────────────────────────────────────────

def _context_block(package: ContextPackage) -> str:
    """Render a ContextPackage into a compact, prompt-ready text block.

    Design rules:
    - Each item is one line: [TYPE] KEY: summary  (short_text if present)
    - Sections only rendered when they have content (no empty headers)
    - Hard cap: max 3 items per section to keep token budget bounded
    - Coverage hints last — they directly guide test targeting
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
        lines.append("Prior Test Cases (same feature area — do NOT copy, use for gap analysis only):")
        for item in package.historical_tests[:3]:
            lines.append(_item_line(item))

    if package.related_stories:
        lines.append("Related Stories (context only — do not inherit their requirements):")
        for item in package.related_stories[:3]:
            lines.append(_item_line(item))

    if package.coverage_hints:
        lines.append("Coverage Hints:")
        for hint in package.coverage_hints:
            lines.append(f"  → {hint}")

    return "\n".join(lines)


# ── Public API ────────────────────────────────────────────────────────────────

def build_prompt(
    story: StoryContext,
    max_tests: int = 10,
    context: ContextPackage | None = None,
    excluded_titles: list[str] | None = None,
) -> str:
    """Return the full prompt string to send to Gemini.

    Two modes:
      baseline  (context=None)  — story only, no historical context
      enriched  (context=...)   — story + ContextPackage with authority rules

    The authority hierarchy is enforced explicitly in the prompt:
      current story requirements  >  historical context
    Gemini is instructed to use context only for gap analysis, not as a
    source of new requirements.
    """
    linked = ""
    if story.linked_issues:
        items = "\n".join(
            f"  - [{li.issue_type}] {li.key}: {li.summary}"
            for li in story.linked_issues
        )
        linked = f"\nLinked Issues:\n{items}"

    labels     = f"\nLabels: {', '.join(story.labels)}"         if story.labels     else ""
    components = f"\nComponents: {', '.join(story.components)}" if story.components else ""

    hard_rules = _HARD_RULES_TEMPLATE.format(
        priorities=_PRIORITY_VALUES,
        types=_TYPE_VALUES,
        issue_key=story.issue_key,
    )

    has_context = bool(
        context and (
            context.linked_defects
            or context.historical_tests
            or context.related_stories
            or context.coverage_hints
        )
    )

    # ── Build context sections (enriched mode only) ───────────────────────────
    context_block_section = ""
    context_rules_section = ""
    context_task_line     = ""

    if has_context:
        assert context is not None  # narrowed — has_context guarantees this
        context_block_section = f"""\
------------------------------------------------------------
HISTORICAL CONTEXT
------------------------------------------------------------
⚠  Authority rule: the STORY section above outranks this section.
   Use this context to improve completeness — NOT as a source of requirements.

{_context_block(context)}
"""
        context_rules_section = f"""\
------------------------------------------------------------
{_CONTEXT_AUTHORITY_RULES}
"""
        context_task_line = (
            "  - Use Historical Context to avoid duplicating known tests and to target gaps\n"
            "  - Write a regression test if a linked defect is directly relevant to the story scope"
        )

    # ── Mode label (appears at top so it's easy to spot in logs) ─────────────
    mode_label = "MODE: enriched (story + historical context)" if has_context else "MODE: baseline (story only)"

    # ── Existing / already-generated exclusion block ──────────────────────────
    exclusion_section = ""
    if excluded_titles:
        titles_block = "\n".join(f"  - {t}" for t in excluded_titles)
        exclusion_section = f"""\
------------------------------------------------------------
ALREADY COVERED TESTS — DO NOT REPEAT
------------------------------------------------------------
The following test case titles already exist on the story or were generated earlier.
Do NOT generate any test that duplicates or closely paraphrases these titles.
Focus on coverage areas and scenarios that are NOT yet covered by these tests.

{titles_block}

"""

    return f"""You are a senior QA engineer generating test cases from a Jira story.
Your output will be parsed by a machine. Any text outside the JSON object will cause a failure.
{mode_label}

------------------------------------------------------------
STORY  ◀ PRIMARY SOURCE OF TRUTH
------------------------------------------------------------
Issue Key : {story.issue_key}
Summary   : {story.summary}

Description:
{story.description or "(not provided)"}

Acceptance Criteria:
{story.acceptance_criteria or "(not provided)"}
{labels}{components}{linked}

{context_block_section}{exclusion_section}------------------------------------------------------------
TASK
------------------------------------------------------------
Generate exactly {max_tests} distinct test cases for the story above.

Requirements:
  - Include AT LEAST ONE test with test_type = "Negative" (not just "Edge Case" — a true negative path)
  - Every test must be grounded in the STORY section — no invented behaviour
  - Return the full requested count unless the story truly lacks enough distinct, grounded scenarios
  - coverage_tag must reference the AC or feature area each test covers (e.g. AC-1, AC-2)
  - Steps must describe what a tester actually does, not what the system should do
{context_task_line}

{context_rules_section}------------------------------------------------------------
{hard_rules}

------------------------------------------------------------
OUTPUT SCHEMA (fill in all fields exactly as shown)
------------------------------------------------------------
{_schema_block(story.issue_key)}"""
