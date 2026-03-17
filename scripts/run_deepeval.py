"""Step 8 — Content-aware DeepEval checks.

Adds two quality gates beyond JSON correctness:

  A. AnswerRelevancy  — are the generated test cases on-topic for the story?
  B. Faithfulness     — does the enriched output stay grounded in the context?

Design choices (quota-friendly):
  • One LLMTestCase per output file (not per individual test), so we
    send at most 2 evaluation calls to the judge LLM.
  • Judge model: gemini-2.0-flash  (cheap, non-thinking, no quota spike).
    This is deliberately different from the generator model.
  • Faithfulness is only meaningful for the enriched output (it needs a
    retrieval_context). Baseline gets AnswerRelevancy only.
  • Thresholds are lenient on Day 3 — the point is to establish the gate,
    not to achieve perfection immediately.

Usage:
    python scripts/run_deepeval.py AIP-2                   # baseline only
    python scripts/run_deepeval.py AIP-2 --enriched        # enriched only
    python scripts/run_deepeval.py AIP-2 --both            # both (default)
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric
from deepeval.models import GeminiModel
from deepeval.test_case import LLMTestCase

from src.config import settings
from src.models.schemas import ContextPackage, GeneratedTestSuite

# ── Paths ─────────────────────────────────────────────────────────────────────

GENERATED_DIR = Path(__file__).resolve().parents[1] / "data" / "generated"
NORMALIZED_DIR = Path(__file__).resolve().parents[1] / "data" / "normalized"
CONTEXT_DIR = Path(__file__).resolve().parents[1] / "data" / "context"

# ── Thresholds ────────────────────────────────────────────────────────────────

# Day 3: intentionally lenient — establishing the gate matters more than
# perfect scores. Tighten on Day 4+ as the pipeline matures.
RELEVANCY_THRESHOLD = 0.5
FAITHFULNESS_THRESHOLD = 0.5

# ── Judge model ───────────────────────────────────────────────────────────────

# gemini-3.1-flash-lite-preview: non-thinking, lowest-cost Gemini 3 model — ideal as
# a judge that just classifies statements. Deliberately NOT gemini-3-flash-preview
# (the thinking model) to avoid the 8k thinking-token overhead on eval calls.
_JUDGE_MODEL_NAME = "gemini-3.1-flash-lite-preview"


def _get_judge() -> GeminiModel:
    # deepeval's GeminiModel reads GOOGLE_API_KEY from the environment and
    # uses it in preference to the api_key kwarg, which causes 429s when the
    # GOOGLE_API_KEY has no Gemini quota. Remove it for this process so that
    # deepeval falls back to GEMINI_API_KEY / our explicit api_key.
    import os
    os.environ.pop("GOOGLE_API_KEY", None)
    return GeminiModel(
        model=_JUDGE_MODEL_NAME,
        api_key=settings.gemini_api_key.get_secret_value(),
        use_vertexai=False,   # force Gemini Developer API
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_suite(key: str, suffix: str) -> GeneratedTestSuite | None:
    """Load a GeneratedTestSuite from data/generated/<key><suffix>.json."""
    path = GENERATED_DIR / f"{key}{suffix}.json"
    if not path.exists():
        print(f"  ⚠️  File not found: {path.name} — skipping.")
        return None
    return GeneratedTestSuite.model_validate_json(path.read_text())


def _load_context(key: str) -> ContextPackage | None:
    """Load a ContextPackage from data/context/<key>.json if it exists."""
    path = CONTEXT_DIR / f"{key}.json"
    if not path.exists():
        return None
    return ContextPackage.model_validate_json(path.read_text())


def _load_story_input(key: str) -> str:
    """Build a compact story description string from normalized story JSON."""
    path = NORMALIZED_DIR / f"{key}.json"
    if not path.exists():
        return f"Story {key}: (normalized story file not found)"
    raw = json.loads(path.read_text())
    parts = [f"Story {key}: {raw.get('summary', '')}"]
    if desc := raw.get("description"):
        parts.append(f"Description: {desc.strip()}")
    if ac := raw.get("acceptance_criteria"):
        parts.append(f"Acceptance Criteria:\n{ac.strip()}")
    return "\n\n".join(parts)


def _suite_to_output(suite: GeneratedTestSuite) -> str:
    """Flatten all test cases into a single text block for evaluation.

    Each test gets its own numbered section with title + steps + expected result.
    This is what the judge LLM reads as `actual_output`.
    """
    lines: list[str] = []
    for i, tc in enumerate(suite.tests, 1):
        test_type = getattr(tc.test_type, "value", tc.test_type)
        priority = getattr(tc.priority, "value", tc.priority)
        lines.append(f"Test {i}: {tc.title}")
        lines.append(f"  Type: {test_type} | Priority: {priority}")
        lines.append("  Steps:")
        for step in tc.steps:
            lines.append(f"    - {step}")
        lines.append(f"  Expected: {tc.expected_result}")
        lines.append("")
    return "\n".join(lines)


def _context_to_retrieval(pkg: ContextPackage) -> list[str]:
    """Convert a ContextPackage into the retrieval_context list that
    FaithfulnessMetric uses to assess grounding.

    Each ContextItem becomes one entry: its summary + short_text.
    Coverage hints are appended as a final block.
    """
    items: list[str] = []
    for item in pkg.linked_defects + pkg.historical_tests + pkg.related_stories:
        text = f"[{item.key}] {item.issue_type}: {item.summary}"
        if item.short_text:
            text += f" — {item.short_text}"
        items.append(text)
    if pkg.coverage_hints:
        items.append("Coverage hints: " + "; ".join(pkg.coverage_hints))
    return items


# ── Evaluation runners ────────────────────────────────────────────────────────

def _header(title: str) -> None:
    print(f"\n{'=' * 62}")
    print(f"  {title}")
    print(f"{'=' * 62}")


def run_answer_relevancy(
    key: str,
    suite: GeneratedTestSuite,
    story_input: str,
    label: str,
    judge: GeminiModel,
) -> bool:
    """Run AnswerRelevancy for one suite. Returns True if threshold met."""
    _header(f"AnswerRelevancy  [{label}]  — {key}")
    print(f"  Judge : {_JUDGE_MODEL_NAME}")
    print(f"  Tests : {len(suite.tests)}")
    print(f"  Threshold: ≥ {RELEVANCY_THRESHOLD}")
    print()

    test_case = LLMTestCase(
        input=story_input,
        actual_output=_suite_to_output(suite),
    )

    metric = AnswerRelevancyMetric(
        threshold=RELEVANCY_THRESHOLD,
        model=judge,
        include_reason=True,
        verbose_mode=False,
    )

    # measure() does a single LLM call — no batch loop, quota-safe
    metric.measure(test_case)

    score: float = metric.score or 0.0
    passed: bool = bool(metric.success)
    reason = metric.reason or "(no reason provided)"

    icon = "✅" if passed else "❌"
    print(f"  {icon}  Score : {score:.3f}  (threshold {RELEVANCY_THRESHOLD})")
    print(f"  Reason: {textwrap.fill(reason, width=58, subsequent_indent='          ')}")
    return passed


def run_faithfulness(
    key: str,
    suite: GeneratedTestSuite,
    story_input: str,
    context_pkg: ContextPackage,
    judge: GeminiModel,
) -> bool:
    """Run Faithfulness for the enriched suite. Returns True if threshold met."""
    _header(f"Faithfulness  [enriched]  — {key}")
    print(f"  Judge : {_JUDGE_MODEL_NAME}")
    print(f"  Tests : {len(suite.tests)}")
    print(f"  Context items: {len(context_pkg.linked_defects + context_pkg.historical_tests)}")
    print(f"  Threshold: ≥ {FAITHFULNESS_THRESHOLD}")
    print()

    retrieval_context = _context_to_retrieval(context_pkg)

    test_case = LLMTestCase(
        input=story_input,
        actual_output=_suite_to_output(suite),
        retrieval_context=retrieval_context,
    )

    metric = FaithfulnessMetric(
        threshold=FAITHFULNESS_THRESHOLD,
        model=judge,
        include_reason=True,
        verbose_mode=False,
    )

    metric.measure(test_case)

    score: float = metric.score or 0.0
    passed: bool = bool(metric.success)
    reason = metric.reason or "(no reason provided)"

    icon = "✅" if passed else "❌"
    print(f"  {icon}  Score : {score:.3f}  (threshold {FAITHFULNESS_THRESHOLD})")
    print(f"  Reason: {textwrap.fill(reason, width=58, subsequent_indent='          ')}")
    return passed


# ── Summary printer ───────────────────────────────────────────────────────────

def _print_summary(results: dict[str, bool]) -> bool:
    """Print a compact result table. Returns True if all passed."""
    print(f"\n{'=' * 62}")
    print("  SUMMARY")
    print(f"{'=' * 62}")
    all_passed = True
    for check_name, passed in results.items():
        icon = "✅" if passed else "❌"
        print(f"  {icon}  {check_name}")
        if not passed:
            all_passed = False
    print(f"{'=' * 62}")
    if all_passed:
        print("  ✅  All content checks passed.")
    else:
        print("  ❌  One or more content checks failed — review reasons above.")
        print()
        print("  Common actions:")
        print("  answer_relevancy LOW → story input may be too thin; check")
        print("    that normalized/<key>.json has description + AC fields.")
        print("  faithfulness LOW     → enriched tests invent claims not in")
        print("    context; tighten C1-C5 rules in prompt.py.")
    print(f"{'=' * 62}")
    return all_passed


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]
    if not args:
        print("Usage: python scripts/run_deepeval.py <ISSUE_KEY> [--both|--enriched]")
        print("       Default: --both")
        sys.exit(1)

    issue_key = args[0].upper()
    mode_flags = {a.lstrip("-") for a in args[1:]}
    run_baseline = "enriched" not in mode_flags          # default on unless --enriched only
    run_enriched_mode = "baseline" not in mode_flags     # default on unless --baseline only

    story_input = _load_story_input(issue_key)
    judge = _get_judge()

    results: dict[str, bool] = {}

    # ── A. Answer Relevancy — Baseline ────────────────────────────────────────
    if run_baseline:
        suite = _load_suite(issue_key, "_baseline")
        if suite:
            passed = run_answer_relevancy(
                issue_key, suite, story_input, "baseline", judge
            )
            results["answer_relevancy [baseline]"] = passed
        else:
            results["answer_relevancy [baseline]"] = False

    # ── A. Answer Relevancy — Enriched ────────────────────────────────────────
    if run_enriched_mode:
        suite_e = _load_suite(issue_key, "_enriched")
        if suite_e:
            passed = run_answer_relevancy(
                issue_key, suite_e, story_input, "enriched", judge
            )
            results["answer_relevancy [enriched]"] = passed
        else:
            results["answer_relevancy [enriched]"] = False

        # ── B. Faithfulness — Enriched only (needs retrieval context) ─────────
        context_pkg = _load_context(issue_key)
        if suite_e and context_pkg:
            passed = run_faithfulness(
                issue_key, suite_e, story_input, context_pkg, judge
            )
            results["faithfulness    [enriched]"] = passed
        elif suite_e:
            print(
                f"\n  ⚠️  Faithfulness skipped — no context file at "
                f"data/context/{issue_key}.json.\n"
                f"     Run: python scripts/collect_context.py {issue_key}"
            )

    all_passed = _print_summary(results)
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
