"""Generate test cases for a normalized story and save the output.

Usage:
    python scripts/generate_tests.py AIP-2

Reads:  data/normalized/<ISSUE_KEY>.json
Writes: data/generated/<ISSUE_KEY>.json
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.generation.generator import generate_test_suite
from src.models.schemas import StoryContext

NORMALIZED_DIR = Path(__file__).resolve().parents[1] / "data" / "normalized"
GENERATED_DIR = Path(__file__).resolve().parents[1] / "data" / "generated"


def main(issue_key: str) -> None:
    normalized_path = NORMALIZED_DIR / f"{issue_key}.json"
    if not normalized_path.exists():
        print(f"ERROR: No normalized story found at {normalized_path}")
        print(f"Run: python scripts/fetch_issue.py {issue_key}  first.")
        sys.exit(1)

    story = StoryContext.model_validate_json(normalized_path.read_text())
    print(f"Generating test cases for {issue_key}...")

    suite = generate_test_suite(story, max_tests=5)

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    output_path = GENERATED_DIR / f"{issue_key}.json"
    output_path.write_text(suite.model_dump_json(indent=2))

    print(f"\nGenerated {len(suite.tests)} test case(s):")
    for i, tc in enumerate(suite.tests, 1):
        print(f"  {i}. [{tc.test_type}] {tc.title}")

    print(f"\nSaved → {output_path}")

    # Quick contract check
    types_present = {tc.test_type for tc in suite.tests}
    from src.models.schemas import CaseType
    if CaseType.NEGATIVE not in types_present:
        print("\n⚠️  WARNING: No negative test case generated — prompt may need adjustment.")
    else:
        print("✅  At least one negative test present.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/generate_tests.py <ISSUE_KEY>")
        sys.exit(1)
    main(sys.argv[1].upper())
