"""Inline evaluation gate — fast, synchronous checks run by the API
before returning a response. Deliberately lightweight: no LLM calls."""

from src.models.schemas import GeneratedTestSuite


def passes_gate(suite: GeneratedTestSuite) -> bool:
    """Return True if every test case in the suite meets minimum quality thresholds.

    Rules (intentionally simple — this is a structural check, not a semantic one):
      - Suite must contain at least one test.
      - Every test must have a non-empty title.
      - Every test must have at least one step.
      - Every test must have a non-empty expected_result.
      - Every test must have a non-empty source_story.
    """
    if not suite.tests:
        return False

    for tc in suite.tests:
        if not tc.title.strip():
            return False
        if not tc.steps:
            return False
        if not tc.expected_result.strip():
            return False
        if not tc.source_story.strip():
            return False

    return True
