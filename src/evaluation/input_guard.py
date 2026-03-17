"""Pre-generation input quality guard.

Inspects the story and (optionally) the context package BEFORE the LLM is
called and returns an InputGuardReport that tells the caller whether to
proceed, warn, or reject outright.

Decision policy (per signal):
  ┌─────────────────────────────────────┬──────────┐
  │ Signal                              │ Verdict  │
  ├─────────────────────────────────────┼──────────┤
  │ missing_acceptance_criteria         │ BLOCK    │
  │ vague_story                         │ BLOCK    │
  │ weak_context  (enriched mode only)  │ WARN     │
  │ conflicting_requirements            │ WARN     │
  │ insufficient_evidence  (catch-all)  │ BLOCK    │
  └─────────────────────────────────────┴──────────┘

The overall report verdict is the worst single-signal verdict.

Usage:
    from src.evaluation.input_guard import check_input, InputRejectedError

    report = check_input(story, context=package, mode="enriched")
    if report.verdict == Verdict.BLOCK:
        raise InputRejectedError(report)
    # warnings can be surfaced to the caller without blocking
    suite = generate_test_suite(story, context=package)
"""

from __future__ import annotations

import re
from typing import Literal

from src.models.schemas import (
    ContextPackage,
    InputGuardReport,
    InputSignal,
    InputSignalResult,
    StoryContext,
    Verdict,
)

# ── Thresholds (easy to tune without touching logic) ─────────────────────────

# Minimum unique meaningful words (>3 chars, non-stopword) in combined story text
_MIN_STORY_WORDS: int = 15

# Minimum characters in combined description + AC to be considered non-vague
_MIN_STORY_CHARS: int = 60

# Minimum number of context items (across all categories) for enriched mode
_MIN_CONTEXT_ITEMS: int = 1

# Minimum total evidence score to proceed (see _evidence_score)
_MIN_EVIDENCE_SCORE: float = 0.25

# Stopwords excluded from word-count check
_STOPWORDS: frozenset[str] = frozenset(
    "the a an is are was were be been being have has had do does did will would "
    "could should may might shall can this that these those it its i we you he "
    "she they them their our your my his her its when where what which who how "
    "and or but if then so as at by for in of on to up with from".split()
)

# Negation patterns used for conflict detection
_NEGATION_RE = re.compile(
    r"\b(must not|should not|shall not|cannot|can not|will not|won't|don't|"
    r"does not|do not|is not|are not|not (be|have|show|display|allow|accept|"
    r"contain|include|require))\b",
    re.I,
)
_AFFIRMATION_RE = re.compile(
    r"\b(must|should|shall|will|can|is required to|needs to|has to|"
    r"displays|shows|allows|accepts|contains|includes)\b",
    re.I,
)

# ── Severity ordering ─────────────────────────────────────────────────────────

_SEVERITY: dict[Verdict, int] = {
    Verdict.PASS:  0,
    Verdict.WARN:  1,
    Verdict.BLOCK: 2,
}

_SIGNAL_POLICY: dict[InputSignal, Verdict] = {
    InputSignal.MISSING_AC:               Verdict.BLOCK,
    InputSignal.VAGUE_STORY:              Verdict.BLOCK,
    InputSignal.WEAK_CONTEXT:             Verdict.WARN,
    InputSignal.CONFLICTING_REQUIREMENTS: Verdict.WARN,
    InputSignal.INSUFFICIENT_EVIDENCE:    Verdict.BLOCK,
}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _word_tokens(text: str) -> list[str]:
    """Return meaningful word tokens (>3 chars, not stopwords, lowercase)."""
    return [
        w for w in re.findall(r"[a-z][a-z0-9]{2,}", text.lower())
        if w not in _STOPWORDS
    ]


def _combined_story_text(story: StoryContext) -> str:
    return " ".join(filter(None, [
        story.summary or "",
        story.description or "",
        story.acceptance_criteria or "",
    ]))


# ── Signal checkers ───────────────────────────────────────────────────────────

def _check_missing_ac(story: StoryContext) -> InputSignalResult:
    """BLOCK when there is no acceptance criteria anywhere in the story."""
    ac = (story.acceptance_criteria or "").strip()
    desc = (story.description or "").strip()

    # Heuristic: look for AC-like phrasing in description when ac field is empty
    ac_phrases = re.compile(
        r"\b(acceptance criteria|ac:|given|when|then|must|shall|should"
        r"|verify that|ensure that|the system (must|should|will))\b",
        re.I,
    )

    has_ac = bool(ac) or bool(ac_phrases.search(desc))

    if not has_ac:
        return InputSignalResult(
            signal=InputSignal.MISSING_AC,
            verdict=Verdict.BLOCK,
            detail=(
                "No acceptance criteria found: the 'acceptance_criteria' field is empty "
                "and the description contains no AC-style phrasing. "
                "Test generation requires at least one verifiable condition."
            ),
        )
    return InputSignalResult(
        signal=InputSignal.MISSING_AC,
        verdict=Verdict.PASS,
        detail="Acceptance criteria present.",
    )


def _check_vague_story(story: StoryContext) -> InputSignalResult:
    """BLOCK when the combined story text is too short or sparse to be actionable."""
    combined = _combined_story_text(story)
    char_count = len(combined.strip())
    word_count = len(_word_tokens(combined))

    if char_count < _MIN_STORY_CHARS:
        return InputSignalResult(
            signal=InputSignal.VAGUE_STORY,
            verdict=Verdict.BLOCK,
            detail=(
                f"Story text is too short to generate reliable tests "
                f"({char_count} chars; minimum {_MIN_STORY_CHARS}). "
                "Add a description or acceptance criteria before generating."
            ),
        )
    if word_count < _MIN_STORY_WORDS:
        return InputSignalResult(
            signal=InputSignal.VAGUE_STORY,
            verdict=Verdict.BLOCK,
            detail=(
                f"Story content is too vague: only {word_count} meaningful "
                f"word(s) found (minimum {_MIN_STORY_WORDS}). "
                "The story lacks enough specific detail to derive test steps."
            ),
        )
    return InputSignalResult(
        signal=InputSignal.VAGUE_STORY,
        verdict=Verdict.PASS,
        detail=f"Story has sufficient content ({word_count} meaningful words, {char_count} chars).",
    )


def _check_weak_context(
    context: ContextPackage | None,
    mode: Literal["baseline", "enriched"],
) -> InputSignalResult:
    """WARN when enriched mode is requested but the context package is too thin."""
    if mode != "enriched":
        return InputSignalResult(
            signal=InputSignal.WEAK_CONTEXT,
            verdict=Verdict.PASS,
            detail="Baseline mode — context check skipped.",
        )
    if context is None:
        return InputSignalResult(
            signal=InputSignal.WEAK_CONTEXT,
            verdict=Verdict.WARN,
            detail=(
                "Enriched mode requested but no context package was provided. "
                "The generator will fall back to baseline behaviour. "
                "Run collect_context first for best results."
            ),
        )
    total_items = (
        len(context.linked_defects)
        + len(context.historical_tests)
        + len(context.related_stories)
    )
    if total_items < _MIN_CONTEXT_ITEMS:
        return InputSignalResult(
            signal=InputSignal.WEAK_CONTEXT,
            verdict=Verdict.WARN,
            detail=(
                f"Context package has {total_items} item(s) "
                f"(minimum {_MIN_CONTEXT_ITEMS} for enriched mode to add value). "
                "Results may not differ from baseline generation."
            ),
        )
    return InputSignalResult(
        signal=InputSignal.WEAK_CONTEXT,
        verdict=Verdict.PASS,
        detail=f"Context package has {total_items} item(s) — sufficient for enriched mode.",
    )


def _check_conflicting_requirements(story: StoryContext) -> InputSignalResult:
    """WARN when the AC/description contains apparent contradictions.

    Heuristic: find sentences that contain both a strong affirmation and a
    negation targeting the same verb/noun cluster.  This catches common
    copy-paste errors like 'The system must display X' and 'The system must
    not display X' in the same criteria block.
    """
    text = " ".join(filter(None, [
        story.acceptance_criteria or "",
        story.description or "",
    ]))
    if not text.strip():
        return InputSignalResult(
            signal=InputSignal.CONFLICTING_REQUIREMENTS,
            verdict=Verdict.PASS,
            detail="No text to check for conflicts.",
        )

    # Split on sentence boundaries
    sentences = re.split(r"(?<=[.!?\n])\s+", text.strip())
    affirmative_nouns: set[str] = set()
    negated_nouns:     set[str] = set()

    for sent in sentences:
        # Extract content nouns (>3 chars) near affirmations and negations
        nouns = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", sent)}
        if _NEGATION_RE.search(sent):
            negated_nouns |= nouns
        if _AFFIRMATION_RE.search(sent):
            affirmative_nouns |= nouns

    # Overlap between affirmative and negated noun sets = potential conflict
    conflicts = affirmative_nouns & negated_nouns - _STOPWORDS
    # Filter out trivial/common words that aren't domain nouns
    _TRIVIAL = frozenset("system user page chat widget button input field form".split())
    meaningful_conflicts = conflicts - _TRIVIAL

    if meaningful_conflicts:
        sample = sorted(meaningful_conflicts)[:5]
        return InputSignalResult(
            signal=InputSignal.CONFLICTING_REQUIREMENTS,
            verdict=Verdict.WARN,
            detail=(
                f"Possible conflicting requirements detected. "
                f"These terms appear in both affirmative and negated sentences: "
                f"{', '.join(sample)}. "
                "Review acceptance criteria for contradictions before generating."
            ),
        )
    return InputSignalResult(
        signal=InputSignal.CONFLICTING_REQUIREMENTS,
        verdict=Verdict.PASS,
        detail="No obvious requirement conflicts detected.",
    )


def _evidence_score(
    story: StoryContext,
    context: ContextPackage | None,
) -> float:
    """Compute a 0–1 evidence score from available story and context signals.

    Score components (equally weighted):
      0.25  — has non-empty description
      0.25  — has non-empty acceptance_criteria
      0.25  — has at least one linked issue
      0.25  — has context items (enriched) OR is running baseline (full credit)
    """
    score = 0.0
    if (story.description or "").strip():
        score += 0.25
    if (story.acceptance_criteria or "").strip():
        score += 0.25
    if story.linked_issues:
        score += 0.25

    if context is not None:
        total = (
            len(context.linked_defects)
            + len(context.historical_tests)
            + len(context.related_stories)
        )
        if total > 0:
            score += 0.25
    else:
        # Baseline mode: no context expected → give full credit for this component
        score += 0.25

    return round(score, 2)


def _check_insufficient_evidence(
    story: StoryContext,
    context: ContextPackage | None,
    other_verdicts: list[Verdict],
) -> InputSignalResult:
    """BLOCK when combined evidence is too weak AND other signals already fired.

    Acts as a catch-all: fires when the evidence score is below threshold
    AND at least one other signal is non-PASS (i.e. the story has multiple
    weak dimensions simultaneously).
    """
    score = _evidence_score(story, context)
    other_non_pass = sum(1 for v in other_verdicts if v != Verdict.PASS)

    if score < _MIN_EVIDENCE_SCORE and other_non_pass >= 1:
        return InputSignalResult(
            signal=InputSignal.INSUFFICIENT_EVIDENCE,
            verdict=Verdict.BLOCK,
            detail=(
                f"Combined evidence score is {score:.2f} (minimum {_MIN_EVIDENCE_SCORE}). "
                f"{other_non_pass} other quality signal(s) also failed. "
                "The system does not have enough evidence to produce reliable tests. "
                "Add a description and acceptance criteria, then retry."
            ),
        )
    return InputSignalResult(
        signal=InputSignal.INSUFFICIENT_EVIDENCE,
        verdict=Verdict.PASS,
        detail=f"Evidence score {score:.2f} — sufficient to attempt generation.",
    )


# ── Public API ────────────────────────────────────────────────────────────────

def check_input(
    story: StoryContext,
    context: ContextPackage | None = None,
    mode: Literal["baseline", "enriched"] = "baseline",
) -> InputGuardReport:
    """Run all pre-generation input quality checks.

    Args:
        story:   The normalised StoryContext to evaluate.
        context: ContextPackage for enriched mode, or None for baseline.
        mode:    "baseline" or "enriched" — affects weak_context check.

    Returns:
        InputGuardReport with overall verdict and per-signal results.
        Caller must inspect report.verdict before calling the LLM.
    """
    # Run the four primary signals first
    r_ac      = _check_missing_ac(story)
    r_vague   = _check_vague_story(story)
    r_context = _check_weak_context(context, mode)
    r_conflict = _check_conflicting_requirements(story)

    primary_verdicts = [r_ac.verdict, r_vague.verdict, r_context.verdict, r_conflict.verdict]

    # Catch-all evidence check (uses verdicts of the other four)
    r_evidence = _check_insufficient_evidence(story, context, primary_verdicts)

    all_results = [r_ac, r_vague, r_context, r_conflict, r_evidence]

    # Overall verdict = worst single-signal verdict
    overall = max(
        (r.verdict for r in all_results),
        key=lambda v: _SEVERITY[v],
    )

    # Count non-pass signals for the summary line
    non_pass = [r for r in all_results if r.verdict != Verdict.PASS]
    if overall == Verdict.PASS:
        summary = f"[{story.issue_key}] All input checks passed — safe to generate."
    elif overall == Verdict.WARN:
        signals = ", ".join(r.signal.value for r in non_pass)
        summary = (
            f"[{story.issue_key}] Input warnings ({len(non_pass)}): {signals}. "
            "Generation is allowed but quality may be reduced."
        )
    else:
        signals = ", ".join(r.signal.value for r in non_pass if r.verdict == Verdict.BLOCK)
        summary = (
            f"[{story.issue_key}] Generation BLOCKED ({len(non_pass)} signal(s) failed): "
            f"{signals}."
        )

    return InputGuardReport(
        issue_key=story.issue_key,
        verdict=overall,
        signal_results=all_results,
        summary=summary,
    )


# ── Exception for hard rejections ─────────────────────────────────────────────

class InputRejectedError(Exception):
    """Raised by generate_test_suite when the input guard returns BLOCK.

    Attributes:
        report: The full InputGuardReport explaining why generation was rejected.
    """

    def __init__(self, report: InputGuardReport) -> None:
        self.report = report
        super().__init__(report.summary)
