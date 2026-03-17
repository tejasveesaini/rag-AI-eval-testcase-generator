"""Duplication detection for generated test suites.

Two detection passes:
  1. history_duplicates  — generated test vs retrieved historical tests
                           (grounded_but_duplicate)
  2. intra_suite         — generated test vs other tests in the same suite
                           (near_duplicate_generated)

Both use token-level Jaccard similarity on a 'body' built from the test's
title + steps + expected_result.  This is intentionally simple and fast
(no LLM calls) while being surprisingly effective for test-case text.

Thresholds (can be overridden per call):
  HISTORY_DUP_THRESHOLD   = 0.55  →  grounded_but_duplicate  → BLOCK
  INTRA_SUITE_THRESHOLD   = 0.70  →  near_duplicate_generated → WARN

Usage:
    from src.evaluation.duplication import detect_duplicates, DuplicationReport

    report = detect_duplicates(suite, historical_items)
    for r in report.case_reports:
        print(r.title, r.verdict, r.duplicate_of)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.models.schemas import (
    ContextItem,
    FailureCategory,
    GeneratedTestCase,
    GeneratedTestSuite,
    Verdict,
)

# ── Default thresholds ────────────────────────────────────────────────────────

HISTORY_DUP_THRESHOLD: float = 0.55
INTRA_SUITE_THRESHOLD: float = 0.70


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tokenize(text: str) -> set[str]:
    """Lowercase alphanumeric tokens from a string."""
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _case_body(tc: GeneratedTestCase) -> str:
    return tc.title + " " + " ".join(tc.steps) + " " + tc.expected_result


def _item_body(item: ContextItem) -> str:
    parts = [item.summary]
    if item.short_text:
        parts.append(item.short_text)
    return " ".join(parts)


def jaccard(a: set[str], b: set[str]) -> float:
    """Token-level Jaccard similarity, returns 0 when both sets are empty."""
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class CaseDupResult:
    """Duplication verdict for one test case."""
    title:        str
    verdict:      Verdict
    category:     FailureCategory | None
    duplicate_of: str | None   # title / key of the matched item, or None
    similarity:   float        # Jaccard score of the worst match found


@dataclass
class DuplicationReport:
    """Full duplication report for a suite."""
    story_key:    str
    case_reports: list[CaseDupResult] = field(default_factory=list)
    # Convenience counters
    clean_count:  int = 0
    warn_count:   int = 0
    block_count:  int = 0

    def summary(self) -> str:
        total = self.clean_count + self.warn_count + self.block_count
        return (
            f"{total} test(s) checked for duplication: "
            f"{self.clean_count} clean / {self.warn_count} warn / "
            f"{self.block_count} block"
        )


# ── Main detection function ───────────────────────────────────────────────────

def detect_duplicates(
    suite: GeneratedTestSuite,
    historical_items: list[ContextItem] | None = None,
    *,
    history_threshold: float = HISTORY_DUP_THRESHOLD,
    intra_threshold: float   = INTRA_SUITE_THRESHOLD,
) -> DuplicationReport:
    """Detect duplicate test cases in *suite* against history and each other.

    Pass 1 — history duplicates:
        For each generated test, compute Jaccard against every historical
        ContextItem body.  If similarity ≥ history_threshold → BLOCK
        (grounded_but_duplicate).

    Pass 2 — intra-suite near-duplicates:
        For each pair of generated tests, compute Jaccard.  If similarity ≥
        intra_threshold, flag the *later* test → WARN
        (near_duplicate_generated).

    Args:
        suite:              The generated test suite to inspect.
        historical_items:   ContextItems from retrieval (historical_test items
                            are most relevant, but all are checked).
        history_threshold:  Jaccard threshold for history duplicate.
        intra_threshold:    Jaccard threshold for intra-suite duplicate.

    Returns:
        DuplicationReport with a CaseDupResult per test case.
    """
    report = DuplicationReport(story_key=suite.story_key)

    # Pre-tokenise generated tests
    generated_bodies  = [_case_body(tc)          for tc in suite.tests]
    generated_toks    = [_tokenize(b)             for b in generated_bodies]

    # Pre-tokenise historical items
    hist_bodies = [_item_body(item) for item in (historical_items or [])]
    hist_keys   = [(item.key + " — " + item.summary) for item in (historical_items or [])]
    hist_toks   = [_tokenize(b) for b in hist_bodies]

    # Initial verdict per test = PASS
    verdicts:    list[Verdict]              = [Verdict.PASS]  * len(suite.tests)
    categories:  list[FailureCategory | None] = [None]        * len(suite.tests)
    dup_of:      list[str | None]           = [None]          * len(suite.tests)
    similarities: list[float]              = [0.0]            * len(suite.tests)

    # ── Pass 1: history duplicates ────────────────────────────────────────────
    for i, (tc_toks, tc) in enumerate(zip(generated_toks, suite.tests)):
        for h_toks, h_label in zip(hist_toks, hist_keys):
            sim = jaccard(tc_toks, h_toks)
            if sim >= history_threshold and sim > similarities[i]:
                verdicts[i]     = Verdict.BLOCK
                categories[i]   = FailureCategory.GROUNDED_BUT_DUPLICATE
                dup_of[i]       = h_label
                similarities[i] = sim

    # ── Pass 2: intra-suite near-duplicates ───────────────────────────────────
    for i in range(len(suite.tests)):
        if verdicts[i] == Verdict.BLOCK:
            continue   # already blocked; skip to avoid double-flagging
        for j in range(i + 1, len(suite.tests)):
            sim = jaccard(generated_toks[i], generated_toks[j])
            if sim >= intra_threshold and sim > similarities[j]:
                # Flag the later test only (keep the first occurrence)
                verdicts[j]     = Verdict.WARN
                categories[j]   = FailureCategory.NEAR_DUPLICATE_GENERATED
                dup_of[j]       = suite.tests[i].title
                similarities[j] = sim

    # ── Build report ──────────────────────────────────────────────────────────
    for i, tc in enumerate(suite.tests):
        res = CaseDupResult(
            title=tc.title,
            verdict=verdicts[i],
            category=categories[i],
            duplicate_of=dup_of[i],
            similarity=similarities[i],
        )
        report.case_reports.append(res)
        if res.verdict == Verdict.PASS:
            report.clean_count += 1
        elif res.verdict == Verdict.WARN:
            report.warn_count += 1
        else:
            report.block_count += 1

    return report
