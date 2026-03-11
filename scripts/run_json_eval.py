"""DeepEval JSON correctness evaluation on generated test suites.

What this checks (JSON correctness = structural + contract validity):
  1. File is valid JSON
  2. Parses into a GeneratedTestSuite without Pydantic errors
  3. Every required field is present and non-empty
  4. Enum values (priority, test_type) are valid
  5. At least one Negative test exists
  6. No test has fewer than 1 step
  7. Every test has a source_story that matches the suite story_key
  8. Passes the inline gate

Usage:
    python scripts/run_json_eval.py AIP-2
    python scripts/run_json_eval.py          # evaluates all files in data/generated/
"""

import json
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.evaluation.gate import passes_gate
from src.models.schemas import CaseType, GeneratedTestSuite, Priority

GENERATED_DIR = Path(__file__).resolve().parents[1] / "data" / "generated"


# ── Individual checks ─────────────────────────────────────────────────────────

class CheckResult(NamedTuple):
    name: str
    passed: bool
    detail: str = ""


def check_valid_json(path: Path) -> CheckResult:
    try:
        json.loads(path.read_text())
        return CheckResult("valid_json", True)
    except json.JSONDecodeError as e:
        return CheckResult("valid_json", False, str(e))


def check_schema_valid(path: Path) -> tuple[CheckResult, GeneratedTestSuite | None]:
    try:
        suite = GeneratedTestSuite.model_validate_json(path.read_text())
        return CheckResult("schema_valid", True), suite
    except Exception as e:
        return CheckResult("schema_valid", False, str(e)), None


def check_no_empty_fields(suite: GeneratedTestSuite) -> CheckResult:
    for i, tc in enumerate(suite.tests):
        if not tc.title.strip():
            return CheckResult("no_empty_fields", False, f"tests[{i}].title is empty")
        if not tc.expected_result.strip():
            return CheckResult("no_empty_fields", False, f"tests[{i}].expected_result is empty")
        if not tc.source_story.strip():
            return CheckResult("no_empty_fields", False, f"tests[{i}].source_story is empty")
    return CheckResult("no_empty_fields", True)


def check_valid_enums(suite: GeneratedTestSuite) -> CheckResult:
    valid_priorities = {p.value for p in Priority}
    valid_types = {t.value for t in CaseType}
    for i, tc in enumerate(suite.tests):
        if tc.priority not in valid_priorities:
            return CheckResult("valid_enums", False, f"tests[{i}].priority='{tc.priority}' not in {valid_priorities}")
        if tc.test_type not in valid_types:
            return CheckResult("valid_enums", False, f"tests[{i}].test_type='{tc.test_type}' not in {valid_types}")
    return CheckResult("valid_enums", True)


def check_has_negative_test(suite: GeneratedTestSuite) -> CheckResult:
    has_negative = any(tc.test_type == CaseType.NEGATIVE for tc in suite.tests)
    if not has_negative:
        return CheckResult("has_negative_test", False, "No test with test_type='Negative' found")
    return CheckResult("has_negative_test", True)


def check_steps_not_empty(suite: GeneratedTestSuite) -> CheckResult:
    for i, tc in enumerate(suite.tests):
        if not tc.steps:
            return CheckResult("steps_not_empty", False, f"tests[{i}].steps is empty")
        for j, step in enumerate(tc.steps):
            if not step.strip():
                return CheckResult("steps_not_empty", False, f"tests[{i}].steps[{j}] is blank")
    return CheckResult("steps_not_empty", True)


def check_source_story_matches(suite: GeneratedTestSuite) -> CheckResult:
    for i, tc in enumerate(suite.tests):
        if tc.source_story != suite.story_key:
            return CheckResult(
                "source_story_matches",
                False,
                f"tests[{i}].source_story='{tc.source_story}' != suite.story_key='{suite.story_key}'",
            )
    return CheckResult("source_story_matches", True)


def check_inline_gate(suite: GeneratedTestSuite) -> CheckResult:
    if passes_gate(suite):
        return CheckResult("inline_gate", True)
    return CheckResult("inline_gate", False, "passes_gate() returned False")


# ── Runner ────────────────────────────────────────────────────────────────────

def evaluate_file(path: Path) -> bool:
    print(f"\n{'='*60}")
    print(f"Evaluating: {path.name}")
    print(f"{'='*60}")

    results: list[CheckResult] = []

    # Check 1: valid JSON
    r = check_valid_json(path)
    results.append(r)
    if not r.passed:
        _print_results(results)
        return False

    # Check 2: schema valid — gates all subsequent checks
    schema_result, suite = check_schema_valid(path)
    results.append(schema_result)
    if not schema_result.passed or suite is None:
        _print_results(results)
        return False

    # Checks 3–7: all require a valid suite
    results.extend([
        check_no_empty_fields(suite),
        check_valid_enums(suite),
        check_has_negative_test(suite),
        check_steps_not_empty(suite),
        check_source_story_matches(suite),
        check_inline_gate(suite),
    ])

    _print_results(results)
    return all(r.passed for r in results)


def _print_results(results: list[CheckResult]) -> None:
    for r in results:
        icon = "✅" if r.passed else "❌"
        detail = f"  → {r.detail}" if r.detail else ""
        print(f"  {icon}  {r.name}{detail}")


def main() -> None:
    if len(sys.argv) == 2:
        paths = [GENERATED_DIR / f"{sys.argv[1].upper()}.json"]
    else:
        paths = sorted(GENERATED_DIR.glob("*.json"))

    if not paths or not paths[0].exists():
        print(f"ERROR: No generated file found. Run scripts/generate_tests.py first.")
        sys.exit(1)

    all_passed = True
    for path in paths:
        passed = evaluate_file(path)
        all_passed = all_passed and passed

    print(f"\n{'='*60}")
    if all_passed:
        print("✅  All checks passed — JSON contract is valid.")
    else:
        print("❌  Some checks failed — see details above.")
        print("\nCommon fixes:")
        print("  invalid_json      → increase max_output_tokens, check for truncation")
        print("  schema_valid      → check required fields in GeneratedTestSuite")
        print("  valid_enums       → prompt must list exact enum values (case-sensitive)")
        print("  has_negative_test → strengthen prompt instruction for Negative test")
        print("  source_story      → add source_story default in generator._parse_suite")
    print(f"{'='*60}")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
