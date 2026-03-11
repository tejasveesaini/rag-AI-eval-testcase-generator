"""Full offline evaluation pipeline using deepeval.
Called by scripts/run_eval.py — never imported by the API."""

from src.models.schemas import GeneratedTestSuite


def run_pipeline(suites: list[GeneratedTestSuite]) -> dict:
    """Run deep evaluation metrics over a batch of test suites.

    Returns a summary dict with metric scores.
    Placeholder — wire in deepeval LLMTestCase + metrics here.
    """
    results = []
    for suite in suites:
        for tc in suite.tests:
            results.append(
                {
                    "story_key": suite.story_key,
                    "title": tc.title,
                    "priority": tc.priority,
                    "test_type": tc.test_type,
                    "steps_count": len(tc.steps),
                    # TODO: add deepeval LLMTestCase + AnswerRelevancyMetric etc.
                }
            )
    return {"evaluated": len(results), "results": results}
