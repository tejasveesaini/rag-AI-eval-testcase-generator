"""Generate test cases for a normalized story and save the output.

Two modes:
  baseline  (default)   — story only, no historical context
  enriched  (--context) — story + ContextPackage from data/context/<key>.json

Usage:
    python scripts/generate_tests.py AIP-2              # baseline
    python scripts/generate_tests.py AIP-2 --context    # enriched

Output files:
    data/generated/<KEY>_baseline.json
    data/generated/<KEY>_enriched.json

Both files are written on each respective run so you can diff them.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.generation.generator import generate_test_suite
from src.models.schemas import CaseType, ContextPackage, StoryContext

NORMALIZED_DIR = Path(__file__).resolve().parents[1] / "data" / "normalized"
CONTEXT_DIR    = Path(__file__).resolve().parents[1] / "data" / "context"
GENERATED_DIR  = Path(__file__).resolve().parents[1] / "data" / "generated"


def _load_context(issue_key: str) -> ContextPackage | None:
    path = CONTEXT_DIR / f"{issue_key}.json"
    if not path.exists():
        print(f"  ⚠  No context package found at {path}")
        print(f"     Run: python scripts/collect_context.py {issue_key}  first.")
        return None
    pkg = ContextPackage.model_validate_json(path.read_text())
    n = len(pkg.linked_defects) + len(pkg.historical_tests) + len(pkg.related_stories)
    print(f"  ✓ Context loaded: {n} item(s), {len(pkg.coverage_hints)} hint(s)")
    return pkg


def _print_suite(suite, label: str) -> None:
    types_present = {tc.test_type for tc in suite.tests}
    has_negative  = CaseType.NEGATIVE in types_present or CaseType.EDGE_CASE in types_present
    print(f"\n  [{label}] {len(suite.tests)} test case(s) generated:")
    for i, tc in enumerate(suite.tests, 1):
        print(f"    {i}. [{tc.test_type.value:12}] [{tc.priority.value:6}] {tc.title}")
    if not has_negative:
        print("  ⚠  WARNING: No Negative or Edge Case test generated.")
    else:
        print("  ✅ At least one Negative/Edge Case test present.")


def main(issue_key: str, use_context: bool) -> None:
    normalized_path = NORMALIZED_DIR / f"{issue_key}.json"
    if not normalized_path.exists():
        print(f"ERROR: No normalized story at {normalized_path}")
        print(f"       Run: python scripts/fetch_issue.py {issue_key}  first.")
        sys.exit(1)

    story = StoryContext.model_validate_json(normalized_path.read_text())
    mode  = "enriched" if use_context else "baseline"
    print(f"\nGenerating [{mode}] test cases for {issue_key} …")
    print(f"  Story: {story.summary[:70]}")

    context: ContextPackage | None = None
    if use_context:
        context = _load_context(issue_key)

    suite = generate_test_suite(story, max_tests=5, context=context)

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = GENERATED_DIR / f"{issue_key}_{mode}.json"
    out_path.write_text(suite.model_dump_json(indent=2))

    _print_suite(suite, mode)
    print(f"\n  Saved → {out_path.relative_to(Path(__file__).resolve().parents[1])}")


def _compare(issue_key: str) -> None:
    """Print a side-by-side title comparison if both output files exist."""
    b_path = GENERATED_DIR / f"{issue_key}_baseline.json"
    e_path = GENERATED_DIR / f"{issue_key}_enriched.json"
    if not b_path.exists() or not e_path.exists():
        return
    b = StoryContext.model_validate_json  # unused — just reading JSON directly
    b_tests = json.loads(b_path.read_text())["tests"]
    e_tests = json.loads(e_path.read_text())["tests"]
    max_rows = max(len(b_tests), len(e_tests))
    print(f"\n{'─'*80}")
    print(f"  COMPARISON  {issue_key}")
    print(f"  {'BASELINE':<38}  {'ENRICHED':<38}")
    print(f"{'─'*80}")
    for i in range(max_rows):
        b_title = b_tests[i]["title"][:36] if i < len(b_tests) else "(none)"
        e_title = e_tests[i]["title"][:36] if i < len(e_tests) else "(none)"
        marker  = "≠" if b_title != e_title else "="
        print(f"  {b_title:<38}{marker} {e_title:<38}")
    print(f"{'─'*80}\n")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        print("Usage: python scripts/generate_tests.py <ISSUE_KEY> [--context] [--compare]")
        sys.exit(1)

    issue_key   = args[0].upper()
    use_context = "--context" in args
    compare     = "--compare" in args

    main(issue_key, use_context)

    if compare:
        _compare(issue_key)
