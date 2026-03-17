"""Step 9 — Failure taxonomy inspector.

Purpose
-------
After content-aware eval (Step 8) you know the *aggregate* score.
This script tells you *why* — by classifying every generated test case
into one or more failure categories.

This is pure rule-based analysis — no extra LLM call, no quota cost.

Failure categories
------------------
CONTEXT_OVERREACH
    The test introduces a concept (browser name, bug key, feature) that
    comes from the context package but is *not* mentioned in the story
    description or acceptance criteria.  Signals the context is being
    taken as spec rather than as hint.

GENERIC_OUTPUT
    The test steps and expected result contain no concrete domain noun
    from the story (e.g. "disclaimer", "credit card", "financial data").
    The test would read equally well for any chat feature — it adds no
    signal.

DUPLICATE_IN_SUITE
    Two or more tests in the same suite have overlapping titles (≥ 60 %
    shared tokens).  Wastes test slots and inflates coverage numbers.

MISSING_NEGATIVE
    The entire suite has no test of type "Negative".  A suite without a
    negative path is incomplete for any story that involves user input.

UNSUPPORTED_ASSUMPTION
    The expected result asserts a behaviour that is neither in the story
    AC nor derivable from the context.  Detected by looking for
    forward-reference phrases ("according to spec", "as specified",
    "per requirements", "as per") — these are proxy signals that the
    model invented a requirement.

HISTORICAL_DUPLICATION
    An enriched test title is too similar (≥ 60 % token overlap) to a
    historical test already present in the context package.  Signals the
    model copied an old test case instead of generating a new one.

Usage
-----
    python scripts/inspect_failures.py AIP-2           # both modes
    python scripts/inspect_failures.py AIP-2 --baseline
    python scripts/inspect_failures.py AIP-2 --enriched
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.models.schemas import (
    ContextPackage,
    GeneratedTestCase,
    GeneratedTestSuite,
)

# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parents[1]
GENERATED_DIR = ROOT / "data" / "generated"
NORMALIZED_DIR = ROOT / "data" / "normalized"
CONTEXT_DIR = ROOT / "data" / "context"

# ── Failure taxonomy ──────────────────────────────────────────────────────────

CONTEXT_OVERREACH = "CONTEXT_OVERREACH"
GENERIC_OUTPUT = "GENERIC_OUTPUT"
DUPLICATE_IN_SUITE = "DUPLICATE_IN_SUITE"
MISSING_NEGATIVE = "MISSING_NEGATIVE"
UNSUPPORTED_ASSUMPTION = "UNSUPPORTED_ASSUMPTION"
HISTORICAL_DUPLICATION = "HISTORICAL_DUPLICATION"

# How to read them when fixing the system
_CATEGORY_ADVICE = {
    CONTEXT_OVERREACH: (
        "Context is bleeding into spec. Strengthen rule C1 in prompt.py: "
        "'context explains defects; the story defines requirements.'"
    ),
    GENERIC_OUTPUT: (
        "Test lacks domain specificity. Add a hard rule in prompt.py: "
        "every test must name at least one concrete element from the story "
        "(e.g. the exact disclaimer text keywords)."
    ),
    DUPLICATE_IN_SUITE: (
        "Model recycled a test slot. Add dedup rule to prompt.py: "
        "'each test must cover a distinct scenario not already covered above.'"
    ),
    MISSING_NEGATIVE: (
        "No negative path tested. Rule R5 in prompt.py must be present. "
        "Check that the enum list includes 'Negative' and the rule is enforced."
    ),
    UNSUPPORTED_ASSUMPTION: (
        "Model hallucinated a requirement. Strip vague assertions from "
        "expected_result. Add rule: 'expected_result must be derivable from "
        "the story AC or observed system behaviour only.'"
    ),
    HISTORICAL_DUPLICATION: (
        "Model copied an old test instead of targeting a new gap. "
        "Add context rule: 'do not reproduce historical tests verbatim; "
        "historical tests show what is already covered, not what to copy.'"
    ),
}


# ── Data class for a single finding ───────────────────────────────────────────

@dataclass
class Finding:
    category: str
    test_index: int          # 1-based
    test_title: str
    detail: str
    evidence: str = ""       # the specific substring that triggered the rule


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tokens(text: str) -> set[str]:
    """Lowercase alphabetic tokens, 3+ chars (strips stop words implicitly)."""
    return {w for w in re.findall(r"[a-z]{3,}", text.lower()) if w not in _STOP}


_STOP = {
    "the", "and", "for", "that", "with", "this", "from", "are", "have",
    "has", "not", "when", "chat", "test", "verify", "check", "window",
    "user", "open", "click", "into", "should", "must", "will", "also",
}


def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _full_text(tc: GeneratedTestCase) -> str:
    """All prose fields of a test case concatenated."""
    return " ".join([tc.title, tc.expected_result, *tc.steps, *tc.preconditions])


def _load_suite(key: str, suffix: str) -> GeneratedTestSuite | None:
    path = GENERATED_DIR / f"{key}{suffix}.json"
    if not path.exists():
        return None
    return GeneratedTestSuite.model_validate_json(path.read_text())


def _load_context(key: str) -> ContextPackage | None:
    path = CONTEXT_DIR / f"{key}.json"
    if not path.exists():
        return None
    return ContextPackage.model_validate_json(path.read_text())


def _load_story_keywords(key: str) -> set[str]:
    """Domain keywords extracted from story description + AC."""
    path = NORMALIZED_DIR / f"{key}.json"
    if not path.exists():
        return set()
    raw = json.loads(path.read_text())
    blob = " ".join(filter(None, [raw.get("description"), raw.get("acceptance_criteria")]))
    return _tokens(blob)


def _context_extra_concepts(story_keywords: set[str], pkg: ContextPackage) -> set[str]:
    """Words introduced by context items that are NOT in the story."""
    ctx_words: set[str] = set()
    for item in pkg.linked_defects + pkg.historical_tests + pkg.related_stories:
        ctx_words |= _tokens(item.summary)
        if item.short_text:
            ctx_words |= _tokens(item.short_text)
    return ctx_words - story_keywords


# ── Individual checks ─────────────────────────────────────────────────────────

_ASSUMPTION_RE = re.compile(
    r"\b(according to (?:spec|requirements?|specifications?)|"
    r"as (?:specified|per)|"
    r"per (?:the )?requirements?|"
    r"as required|"
    r"correctly positioned according|"
    r"specified placement)\b",
    re.IGNORECASE,
)

# Concrete domain nouns that must appear in at least one of: title, steps, expected
_DOMAIN_ANCHORS = {
    "disclaimer", "warning", "security", "financial", "credit", "bank",
    "privacy", "data", "input", "message",
}


def check_context_overreach(
    tc: GeneratedTestCase,
    idx: int,
    context_extras: set[str],
) -> Finding | None:
    if not context_extras:
        return None
    test_words = _tokens(_full_text(tc))
    leaked = test_words & context_extras
    if leaked:
        return Finding(
            category=CONTEXT_OVERREACH,
            test_index=idx,
            test_title=tc.title,
            detail=(
                f"Test references concept(s) from context that are absent from the story: "
                f"{sorted(leaked)}"
            ),
            evidence=", ".join(sorted(leaked)),
        )
    return None


def check_generic_output(tc: GeneratedTestCase, idx: int) -> Finding | None:
    test_words = _tokens(_full_text(tc))
    if not (test_words & _DOMAIN_ANCHORS):
        return Finding(
            category=GENERIC_OUTPUT,
            test_index=idx,
            test_title=tc.title,
            detail=(
                "No domain-specific noun found. Expected at least one of: "
                f"{sorted(_DOMAIN_ANCHORS)}"
            ),
            evidence=tc.expected_result,
        )
    return None


def check_unsupported_assumption(tc: GeneratedTestCase, idx: int) -> Finding | None:
    full = _full_text(tc)
    m = _ASSUMPTION_RE.search(full)
    if m:
        return Finding(
            category=UNSUPPORTED_ASSUMPTION,
            test_index=idx,
            test_title=tc.title,
            detail=(
                "Expected result or step uses a forward-reference phrase that "
                "implies an unstated requirement."
            ),
            evidence=m.group(0),
        )
    return None


def check_duplicates(tests: list[GeneratedTestCase], threshold: float = 0.60) -> list[Finding]:
    findings: list[Finding] = []
    seen: list[tuple[int, GeneratedTestCase]] = []
    for idx, tc in enumerate(tests, 1):
        for prev_idx, prev_tc in seen:
            sim = _jaccard(tc.title, prev_tc.title)
            if sim >= threshold:
                findings.append(Finding(
                    category=DUPLICATE_IN_SUITE,
                    test_index=idx,
                    test_title=tc.title,
                    detail=(
                        f"Title overlaps {sim:.0%} with test {prev_idx}: "
                        f"'{prev_tc.title}'"
                    ),
                    evidence=f"Jaccard={sim:.2f}",
                ))
        seen.append((idx, tc))
    return findings


def check_missing_negative(suite: GeneratedTestSuite) -> Finding | None:
    from src.models.schemas import CaseType
    if not any(tc.test_type == CaseType.NEGATIVE for tc in suite.tests):
        return Finding(
            category=MISSING_NEGATIVE,
            test_index=0,
            test_title="(suite level)",
            detail="Suite contains no test with test_type='Negative'.",
            evidence="",
        )
    return None


def check_historical_duplication(
    tc: GeneratedTestCase,
    idx: int,
    historical_titles: list[str],
    threshold: float = 0.60,
) -> Finding | None:
    for hist_title in historical_titles:
        sim = _jaccard(tc.title, hist_title)
        if sim >= threshold:
            return Finding(
                category=HISTORICAL_DUPLICATION,
                test_index=idx,
                test_title=tc.title,
                detail=(
                    f"Title overlaps {sim:.0%} with historical test: '{hist_title}'"
                ),
                evidence=f"Jaccard={sim:.2f}",
            )
    return None


# ── Full suite analysis ───────────────────────────────────────────────────────

def analyse_suite(
    suite: GeneratedTestSuite,
    label: str,
    story_keywords: set[str],
    context_pkg: ContextPackage | None,
) -> list[Finding]:
    findings: list[Finding] = []

    context_extras: set[str] = set()
    historical_titles: list[str] = []
    if context_pkg:
        context_extras = _context_extra_concepts(story_keywords, context_pkg)
        historical_titles = [item.summary for item in context_pkg.historical_tests]

    # per-test checks
    for idx, tc in enumerate(suite.tests, 1):
        if context_pkg:
            if f := check_context_overreach(tc, idx, context_extras):
                findings.append(f)
            if f := check_historical_duplication(tc, idx, historical_titles):
                findings.append(f)

        if f := check_generic_output(tc, idx):
            findings.append(f)
        if f := check_unsupported_assumption(tc, idx):
            findings.append(f)

    # suite-level checks
    findings.extend(check_duplicates(suite.tests))
    if f := check_missing_negative(suite):
        findings.append(f)

    return findings


# ── Printer ───────────────────────────────────────────────────────────────────

def _print_report(
    key: str,
    label: str,
    suite: GeneratedTestSuite,
    findings: list[Finding],
) -> None:
    print(f"\n{'=' * 64}")
    print(f"  Failure Taxonomy Report  [{label}]  — {key}")
    print(f"  {len(suite.tests)} test(s) inspected  |  {len(findings)} finding(s)")
    print(f"{'=' * 64}")

    if not findings:
        print("  ✅  No issues found in this suite.")
        return

    # Group by category
    by_cat: dict[str, list[Finding]] = {}
    for f in findings:
        by_cat.setdefault(f.category, []).append(f)

    for cat, items in sorted(by_cat.items()):
        print(f"\n  ▶  {cat}  ({len(items)} finding{'s' if len(items) > 1 else ''})")
        for f in items:
            loc = f"Test {f.test_index}" if f.test_index else "Suite"
            print(f"     [{loc}] {f.test_title}")
            print(f"       → {f.detail}")
            if f.evidence:
                print(f"       ✎  evidence: «{f.evidence}»")
        print(f"\n     💡 Fix: {_CATEGORY_ADVICE[cat]}")

    # Compact summary row
    cats = sorted(by_cat.keys())
    print(f"\n  Categories triggered: {', '.join(cats)}")


def _print_final_summary(reports: dict[str, list[Finding]]) -> None:
    print(f"\n{'=' * 64}")
    print("  TAXONOMY SUMMARY  —  across all modes")
    print(f"{'=' * 64}")
    all_cats: dict[str, int] = {}
    for label, findings in reports.items():
        count = len(findings)
        icon = "✅" if count == 0 else "⚠️ "
        print(f"  {icon}  [{label}]  {count} finding(s)")
        for f in findings:
            all_cats[f.category] = all_cats.get(f.category, 0) + 1

    if all_cats:
        print()
        print("  Failure categories across all modes (most frequent first):")
        for cat, n in sorted(all_cats.items(), key=lambda x: -x[1]):
            print(f"    {n:2d}×  {cat}")
        print()
        print("  Recommended fix order (highest-impact first):")
        priority_order = [
            UNSUPPORTED_ASSUMPTION,
            CONTEXT_OVERREACH,
            GENERIC_OUTPUT,
            DUPLICATE_IN_SUITE,
            MISSING_NEGATIVE,
            HISTORICAL_DUPLICATION,
        ]
        rank = 1
        for cat in priority_order:
            if cat in all_cats:
                print(f"    {rank}. {cat}")
                print(f"       {_CATEGORY_ADVICE[cat]}")
                rank += 1
    else:
        print()
        print("  ✅  No failure categories triggered across any mode.")
    print(f"{'=' * 64}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]
    if not args:
        print("Usage: python scripts/inspect_failures.py <ISSUE_KEY> [--baseline|--enriched]")
        sys.exit(1)

    issue_key = args[0].upper()
    flags = {a.lstrip("-") for a in args[1:]}
    run_baseline = "enriched" not in flags
    run_enriched = "baseline" not in flags

    story_keywords = _load_story_keywords(issue_key)
    context_pkg = _load_context(issue_key)

    reports: dict[str, list[Finding]] = {}

    if run_baseline:
        suite = _load_suite(issue_key, "_baseline")
        if suite:
            findings = analyse_suite(suite, "baseline", story_keywords, context_pkg=None)
            _print_report(issue_key, "baseline", suite, findings)
            reports["baseline"] = findings
        else:
            print(f"⚠️  data/generated/{issue_key}_baseline.json not found — run generate_tests.py first.")

    if run_enriched:
        suite_e = _load_suite(issue_key, "_enriched")
        if suite_e:
            findings_e = analyse_suite(suite_e, "enriched", story_keywords, context_pkg)
            _print_report(issue_key, "enriched", suite_e, findings_e)
            reports["enriched"] = findings_e
        else:
            print(f"⚠️  data/generated/{issue_key}_enriched.json not found — run generate_tests.py --context first.")

    if reports:
        _print_final_summary(reports)


if __name__ == "__main__":
    main()
