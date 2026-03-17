"""Inline evaluation gate — fast, synchronous checks run by the API
before returning a response.  Deliberately lightweight: no LLM calls.

Decision policy
───────────────
Each known failure category maps to a fixed Verdict:

  FailureCategory               │ Verdict
  ──────────────────────────────┼────────
  MALFORMED_BUT_RELEVANT        │ BLOCK
  GROUNDED_BUT_DUPLICATE        │ BLOCK   (history duplicate → adds no value)
  RELEVANT_BUT_UNSUPPORTED      │ BLOCK   (references non-existent AC/feature)
  SHOULD_REFUSE_GENERATED       │ BLOCK   (empty story → should not generate)
  USEFUL_BUT_GENERIC            │ WARN    (passes structure but is too vague)
  NEAR_DUPLICATE_GENERATED      │ WARN    (intra-suite duplicate, not identical)

A test case verdict is the *worst* verdict across all failures that fired.
The suite verdict is the *worst* verdict across all case verdicts.
"""

from __future__ import annotations

import re
from typing import Sequence

from src.models.schemas import (
    CaseGateResult,
    CaseType,
    ContextItem,
    FailureCategory,
    GeneratedTestCase,
    GeneratedTestSuite,
    StoryContext,
    SuiteGateReport,
    Verdict,
)

# ── Verdict severity ordering ─────────────────────────────────────────────────

_SEVERITY: dict[Verdict, int] = {
    Verdict.PASS:  0,
    Verdict.WARN:  1,
    Verdict.BLOCK: 2,
}

# ── Decision policy: failure category → verdict ───────────────────────────────

DECISION_POLICY: dict[FailureCategory, Verdict] = {
    FailureCategory.MALFORMED_BUT_RELEVANT:   Verdict.BLOCK,
    FailureCategory.GROUNDED_BUT_DUPLICATE:   Verdict.BLOCK,
    FailureCategory.RELEVANT_BUT_UNSUPPORTED: Verdict.BLOCK,
    FailureCategory.SHOULD_REFUSE_GENERATED:  Verdict.BLOCK,
    FailureCategory.USEFUL_BUT_GENERIC:       Verdict.WARN,
    FailureCategory.NEAR_DUPLICATE_GENERATED: Verdict.WARN,
}

# ── Generic-test heuristics ───────────────────────────────────────────────────

_GENERIC_STEP_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bnavigate to\b", re.I),
    re.compile(r"\bopen the (app|application|page|browser)\b", re.I),
    re.compile(r"\benter (valid|invalid)? ?(username|password|credentials)\b", re.I),
    re.compile(r"\bclick (the )?(submit|button|ok|cancel)\b", re.I),
    re.compile(r"\bverify (the )?(result|output|response)\b", re.I),
    re.compile(r"\bobserve (the )?(result|output|page)\b", re.I),
]

_GENERIC_TITLE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^test that\b", re.I),
    re.compile(r"^verify (that )?(the )?(system|app|application|feature)\b", re.I),
    re.compile(r"^check (that )?(the )?(system|functionality)\b", re.I),
]

_MIN_STEP_LENGTH = 15   # steps shorter than this are likely boilerplate
_GENERIC_STEP_THRESHOLD = 0.5   # >50 % of steps match generic patterns → warn


def _is_generic(tc: GeneratedTestCase) -> bool:
    """Return True if the test looks like boilerplate (useful but too vague)."""
    # Title matches a generic pattern
    if any(p.match(tc.title.strip()) for p in _GENERIC_TITLE_PATTERNS):
        return True

    # Majority of steps are very short or match generic patterns
    if not tc.steps:
        return False
    generic_count = sum(
        1 for s in tc.steps
        if len(s.strip()) < _MIN_STEP_LENGTH
        or any(p.search(s) for p in _GENERIC_STEP_PATTERNS)
    )
    return (generic_count / len(tc.steps)) >= _GENERIC_STEP_THRESHOLD


# ── Unsupported-claim detection ───────────────────────────────────────────────

def _story_tokens(story: StoryContext) -> frozenset[str]:
    """Build a set of lowercase word tokens from all story text fields."""
    text = " ".join(filter(None, [
        story.summary,
        story.description or "",
        story.acceptance_criteria or "",
        " ".join(story.labels),
        " ".join(story.components),
    ]))
    return frozenset(re.findall(r"[a-z0-9]+", text.lower()))


def _is_unsupported(tc: GeneratedTestCase, story_tokens: frozenset[str]) -> bool:
    """Return True if the test references concepts absent from the story.

    Heuristic: extract 'domain words' (>4 chars, not stopwords) from title +
    steps + expected_result.  If more than half those words have NO overlap with
    the story, flag the test as referencing unsupported features.
    """
    _STOPWORDS = frozenset(
        "that this with from have will when then should must after before"
        " into onto upon about which there their they them".split()
    )
    tc_text = " ".join([tc.title, *tc.steps, tc.expected_result])
    tc_words = {
        w for w in re.findall(r"[a-z0-9]+", tc_text.lower())
        if len(w) > 4 and w not in _STOPWORDS
    }
    if not tc_words:
        return False
    overlap = tc_words & story_tokens
    return (len(overlap) / len(tc_words)) < 0.15   # <15 % overlap → unsupported


# ── Per-case gate ─────────────────────────────────────────────────────────────

def evaluate_case(
    tc: GeneratedTestCase,
    story: StoryContext,
    story_tokens: frozenset[str],
    historical_bodies: Sequence[str],
    *,
    dup_threshold: float = 0.55,
) -> CaseGateResult:
    """Apply all gate rules to one test case, return a CaseGateResult."""
    failures: list[FailureCategory] = []
    reasons:  list[str]             = []

    # ── Rule 1: malformed_but_relevant ────────────────────────────────────────
    if not tc.title.strip():
        failures.append(FailureCategory.MALFORMED_BUT_RELEVANT)
        reasons.append("title is empty")
    if not tc.steps:
        failures.append(FailureCategory.MALFORMED_BUT_RELEVANT)
        reasons.append("no steps provided")
    if not tc.expected_result.strip():
        failures.append(FailureCategory.MALFORMED_BUT_RELEVANT)
        reasons.append("expected_result is empty")
    if not tc.source_story.strip():
        failures.append(FailureCategory.MALFORMED_BUT_RELEVANT)
        reasons.append("source_story is empty")

    # ── Rule 2: should_refuse_generated ──────────────────────────────────────
    story_has_content = bool(
        (story.description or "").strip()
        or (story.acceptance_criteria or "").strip()
    )
    if not story_has_content:
        failures.append(FailureCategory.SHOULD_REFUSE_GENERATED)
        reasons.append(
            "story has no description or acceptance criteria; "
            "test should not have been generated"
        )

    # ── Rule 3: relevant_but_unsupported ─────────────────────────────────────
    if story_has_content and _is_unsupported(tc, story_tokens):
        failures.append(FailureCategory.RELEVANT_BUT_UNSUPPORTED)
        reasons.append(
            "test references concepts not found in the story "
            "(low token overlap with story text)"
        )

    # ── Rule 4: grounded_but_duplicate (vs historical) ────────────────────────
    tc_body = (tc.title + " " + " ".join(tc.steps) + " " + tc.expected_result).lower()
    tc_toks = set(re.findall(r"[a-z0-9]+", tc_body))
    for hist in historical_bodies:
        hist_toks = set(re.findall(r"[a-z0-9]+", hist.lower()))
        union = tc_toks | hist_toks
        if not union:
            continue
        jaccard = len(tc_toks & hist_toks) / len(union)
        if jaccard >= dup_threshold:
            failures.append(FailureCategory.GROUNDED_BUT_DUPLICATE)
            reasons.append(
                f"Jaccard similarity {jaccard:.2f} with a historical test "
                f"(threshold {dup_threshold})"
            )
            break

    # ── Rule 5: useful_but_generic ────────────────────────────────────────────
    if not failures and _is_generic(tc):
        failures.append(FailureCategory.USEFUL_BUT_GENERIC)
        reasons.append(
            "test passes structural checks but steps/title are boilerplate-level generic"
        )

    # ── Compute verdict ───────────────────────────────────────────────────────
    if not failures:
        verdict = Verdict.PASS
    else:
        verdict = max(
            (DECISION_POLICY[f] for f in failures),
            key=lambda v: _SEVERITY[v],
        )

    return CaseGateResult(
        title=tc.title,
        verdict=verdict,
        failures=failures,
        reasons=reasons,
    )


# ── Suite-level gate ──────────────────────────────────────────────────────────

def evaluate_suite(
    suite: GeneratedTestSuite,
    story: StoryContext,
    historical_items: Sequence[ContextItem] | None = None,
    *,
    dup_threshold: float = 0.55,
    intra_dup_threshold: float = 0.70,
) -> SuiteGateReport:
    """Run the full decision policy over every test in the suite.

    Args:
        suite:              The generated test suite to evaluate.
        story:              The source StoryContext (used for unsupported detection).
        historical_items:   ContextItems from retrieval (historical tests/bugs).
                            Used for grounded-but-duplicate detection.
        dup_threshold:      Jaccard threshold for history-vs-generated duplicate.
        intra_dup_threshold:Jaccard threshold for intra-suite near-duplicate.
    """
    # Build story token set once
    s_tokens = _story_tokens(story)

    # Build historical body strings from ContextItems (test items only)
    hist_bodies: list[str] = []
    for item in (historical_items or []):
        body = item.summary
        if item.short_text:
            body += " " + item.short_text
        hist_bodies.append(body)

    # Evaluate each case
    case_results: list[CaseGateResult] = []
    for tc in suite.tests:
        result = evaluate_case(
            tc, story, s_tokens, hist_bodies, dup_threshold=dup_threshold
        )
        case_results.append(result)

    # ── Intra-suite near-duplicate detection ──────────────────────────────────
    case_bodies = [
        (tc.title + " " + " ".join(tc.steps) + " " + tc.expected_result).lower()
        for tc in suite.tests
    ]
    for i, res_i in enumerate(case_results):
        if res_i.verdict == Verdict.BLOCK:
            continue   # already blocked, skip
        toks_i = set(re.findall(r"[a-z0-9]+", case_bodies[i]))
        for j in range(i + 1, len(case_results)):
            if case_results[j].verdict == Verdict.BLOCK:
                continue
            toks_j = set(re.findall(r"[a-z0-9]+", case_bodies[j]))
            union = toks_i | toks_j
            if not union:
                continue
            jaccard = len(toks_i & toks_j) / len(union)
            if jaccard >= intra_dup_threshold:
                # Flag the later test only (keep the first)
                case_results[j].failures.append(FailureCategory.NEAR_DUPLICATE_GENERATED)
                case_results[j].reasons.append(
                    f"Jaccard {jaccard:.2f} similarity with '{suite.tests[i].title}' "
                    f"(intra-suite, threshold {intra_dup_threshold})"
                )
                # Upgrade verdict if needed
                new_v = DECISION_POLICY[FailureCategory.NEAR_DUPLICATE_GENERATED]
                if _SEVERITY[new_v] > _SEVERITY[case_results[j].verdict]:
                    case_results[j].verdict = new_v

    # ── Suite-level aggregation ───────────────────────────────────────────────
    pass_count  = sum(1 for r in case_results if r.verdict == Verdict.PASS)
    warn_count  = sum(1 for r in case_results if r.verdict == Verdict.WARN)
    block_count = sum(1 for r in case_results if r.verdict == Verdict.BLOCK)

    suite_verdict = max(
        (r.verdict for r in case_results),
        key=lambda v: _SEVERITY[v],
        default=Verdict.PASS,
    )

    summary = (
        f"{len(suite.tests)} test(s) evaluated: "
        f"{pass_count} pass / {warn_count} warn / {block_count} block — "
        f"suite verdict: {suite_verdict.value.upper()}"
    )

    return SuiteGateReport(
        story_key=suite.story_key,
        suite_verdict=suite_verdict,
        case_results=case_results,
        pass_count=pass_count,
        warn_count=warn_count,
        block_count=block_count,
        summary=summary,
    )


# ── Backward-compat helper (used by existing API routes) ─────────────────────

def passes_gate(suite: GeneratedTestSuite) -> bool:
    """Legacy helper — returns False if ANY case would be BLOCKED.

    Kept for backward compatibility with src/api/routes.py.
    New code should call evaluate_suite() and inspect the SuiteGateReport.
    """
    for tc in suite.tests:
        if not tc.title.strip():
            return False
        if not tc.steps:
            return False
        if not tc.expected_result.strip():
            return False
        if not tc.source_story.strip():
            return False
    return bool(suite.tests)
