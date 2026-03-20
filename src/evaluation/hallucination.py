"""Hallucination detector — invented specific values in test cases.

Detects test cases that assert concrete, specific values (numbers, quoted
strings, error codes, time durations, percentage thresholds, etc.) that
appear **nowhere** in the story description, acceptance criteria, or any
provided context items.

The core idea: a model that "knows" the story should only assert specific
values that are grounded in the source material.  When a test says
"the system should respond within 200 ms" or "returns HTTP 403" but neither
value appears in the story or context, the model hallucinated it.

Extraction strategy
───────────────────
From each test's (title + steps + expected_result) we extract:
  • Integer and decimal numbers             (e.g. 200, 3.5, 1000)
  • HTTP-style status codes                 (e.g. 400, 403, 500)
  • Quoted strings                          (e.g. "Invalid input", 'Error')
  • Time expressions                        (e.g. "500ms", "2s", "1 minute")
  • Percentage values                       (e.g. 95%, 0.9)

Each candidate value is then searched (case-insensitive, whole-word for
numbers) in a combined reference text built from:
  story.summary + story.description + story.acceptance_criteria
  + all context item summaries and short_texts.

A value is flagged as hallucinated only if it is:
  (a) not present in the reference text, AND
  (b) non-trivially specific  (>= MIN_VALUE_LENGTH chars, not a stopword number)

Verdict policy:
    no flags         → PASS
    ≥1 flag(s)       → WARN   (blocking would be too aggressive; these need human review)
"""

from __future__ import annotations

import re

from src.models.schemas import (
    ContextItem,
    GeneratedTestSuite,
    HallucinationFlag,
    HallucinationReport,
    StoryContext,
    Verdict,
)

# ── Extraction regexes ────────────────────────────────────────────────────────

# Quoted strings — only full double-quote or full single-quote pairs.
# Deliberately excludes apostrophes inside words (possessives like "user's")
# by requiring the opening quote to be preceded by whitespace, start-of-string,
# or punctuation — not a word character.
_QUOTED_RE = re.compile(r"""(?<!\w)(?:["'])([^"'\n]{2,60})(?:["'])(?!\w)""")

# Standalone numbers: integers, decimals, percentages
# Deliberately excludes single-digit standalone numbers (too noisy)
_NUMBER_RE = re.compile(
    r"""
    \b
    (?:
        \d{2,}(?:\.\d+)?       # integer ≥10 or decimal
        |\d+\.\d+              # any decimal
    )
    (?:\s*%)?                  # optional percentage
    \b
    """,
    re.VERBOSE,
)

# Time expressions: "200ms", "3s", "1 second", "2 minutes", "500 milliseconds"
_TIME_RE = re.compile(
    r"\b\d+\s*(?:ms|milliseconds?|seconds?|minutes?|hours?|s\b)",
    re.I,
)

# HTTP status codes: 3-digit numbers starting with 2, 3, 4, 5
_HTTP_STATUS_RE = re.compile(r"\b[2-5]\d{2}\b")

# Trivially common numbers we ignore (years, common counts that aren't asserted values)
_IGNORED_NUMBERS: frozenset[str] = frozenset(
    str(y) for y in range(2000, 2030)
)
_IGNORED_NUMBERS |= frozenset(["10", "100", "50"])   # too generic to signal hallucination

# Well-known HTTP status codes that are universally understood and don't need
# to be spelled out in the story to be legitimately used in a test.
_COMMON_HTTP_CODES: frozenset[str] = frozenset([
    "200", "201", "204",        # success
    "301", "302", "304",        # redirects
    "400", "401", "403", "404", # client errors
    "409", "422", "429",        # conflict / validation / rate-limit
    "500", "502", "503",        # server errors
])

# Generic UI/UX words that appear in quoted form in tests but are too common
# to be considered hallucinated values (button labels, states, etc.)
_IGNORED_QUOTED: frozenset[str] = frozenset([
    "cancel", "close", "ok", "yes", "no", "submit", "confirm", "save",
    "delete", "back", "next", "done", "edit", "open", "continue",
    "success", "error", "warning", "info", "loading",
])


def _extract_specific_values(text: str) -> list[str]:
    """Extract concrete asserted values from *text*."""
    found: list[str] = []

    # Quoted strings (highest signal) — skip generic UI labels
    for m in _QUOTED_RE.finditer(text):
        val = m.group(1).strip()
        if len(val) >= 2 and val.lower() not in _IGNORED_QUOTED:
            found.append(val)

    # Time expressions (before generic numbers, to avoid double-flagging)
    for m in _TIME_RE.finditer(text):
        found.append(m.group(0).strip())

    # HTTP status codes — skip well-known standard codes
    for m in _HTTP_STATUS_RE.finditer(text):
        val = m.group(0)
        if val not in _IGNORED_NUMBERS and val not in _COMMON_HTTP_CODES:
            found.append(val)

    # Generic numbers (deduplicate with already-found time/HTTP values)
    already = {v.lower() for v in found}
    for m in _NUMBER_RE.finditer(text):
        val = m.group(0).strip()
        if val in _IGNORED_NUMBERS:
            continue
        if val.lower() not in already:
            found.append(val)
            already.add(val.lower())

    # Deduplicate preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for v in found:
        key = v.lower()
        if key not in seen:
            seen.add(key)
            unique.append(v)

    return unique


def _build_reference_text(
    story: StoryContext,
    context_items: list[ContextItem] | None,
) -> str:
    """Build a single lowercase reference corpus from story + context."""
    parts: list[str] = [
        story.summary or "",
        story.description or "",
        story.acceptance_criteria or "",
    ]
    for item in context_items or []:
        parts.append(item.summary or "")
        if item.short_text:
            parts.append(item.short_text)
    return " ".join(parts).lower()


def _value_in_reference(value: str, reference: str) -> bool:
    """Return True if *value* can be found in *reference* (word-boundary aware)."""
    escaped = re.escape(value.lower())
    # For purely numeric values use word boundary; for quoted strings substring match
    if re.fullmatch(r"[\d\s.%]+", value):
        pattern = r"\b" + escaped + r"\b"
    else:
        pattern = re.escape(value.lower())
    return bool(re.search(pattern, reference, re.I))


# ── Public API ────────────────────────────────────────────────────────────────

def check_hallucination(
    suite: GeneratedTestSuite,
    story: StoryContext,
    context_items: list[ContextItem] | None = None,
) -> HallucinationReport:
    """Detect test cases that assert specific values not found in source material.

    Args:
        suite:          The generated test suite to inspect.
        story:          Source StoryContext — supplies the reference text.
        context_items:  All ContextItems from retrieval (summaries + short_text
                        are added to the reference corpus).

    Returns:
        HallucinationReport with a HallucinationFlag per offending test case.
    """
    report = HallucinationReport(story_key=suite.story_key)

    reference = _build_reference_text(story, context_items)

    flags: list[HallucinationFlag] = []
    clean = 0

    for idx, tc in enumerate(suite.tests):
        tc_text = " ".join([tc.title, *tc.steps, tc.expected_result])
        candidates = _extract_specific_values(tc_text)

        hallucinated = [
            val for val in candidates
            if not _value_in_reference(val, reference)
        ]

        if hallucinated:
            flags.append(
                HallucinationFlag(
                    test_index=idx,
                    title=tc.title,
                    hallucinated_values=hallucinated,
                    reason=(
                        f"Asserts {len(hallucinated)} specific value(s) not found in "
                        f"story or context: {', '.join(repr(v) for v in hallucinated[:5])}"
                        + (" …" if len(hallucinated) > 5 else "")
                    ),
                )
            )
        else:
            clean += 1

    flagged = len(flags)
    verdict = Verdict.WARN if flagged else Verdict.PASS
    summary = (
        f"{clean}/{len(suite.tests)} test(s) clean — "
        f"{flagged} test(s) assert ungrounded specific value(s)."
        if flagged
        else f"All {len(suite.tests)} test(s) — no hallucinated specific values detected."
    )

    report.flags = flags
    report.clean_count = clean
    report.flagged_count = flagged
    report.verdict = verdict
    report.summary = summary
    return report
