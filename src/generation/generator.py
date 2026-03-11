"""Gemini-based test-case generator.

Single responsibility: send a prompt to Gemini, parse the JSON response,
and return a validated GeneratedTestSuite.

Nothing in here touches Jira or the API layer.
"""

import json
import re

from google import genai
from google.genai import types

from src.config import get_settings
from src.generation.prompt import build_prompt
from src.models.schemas import GeneratedTestSuite, StoryContext


# Model to use — confirmed available via API
_MODEL = "gemini-3-flash-preview"

# Strip markdown code fences if Gemini wraps output despite instructions
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```")


def _parse_suite(raw_text: str, issue_key: str) -> GeneratedTestSuite:
    """Extract and validate a GeneratedTestSuite from Gemini's raw text output."""
    text = raw_text.strip()

    # Strip code fences if present
    fence_match = _CODE_FENCE_RE.search(text)
    if fence_match:
        text = fence_match.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Gemini returned non-JSON output.\n"
            f"Parse error: {e}\n"
            f"Raw output (first 500 chars):\n{raw_text[:500]}"
        )

    # Ensure source_story is set on every test case (model may omit it)
    for tc in data.get("tests", []):
        tc.setdefault("source_story", issue_key)

    return GeneratedTestSuite.model_validate(data)


def generate_test_suite(story: StoryContext, max_tests: int = 5) -> GeneratedTestSuite:
    """Call Gemini synchronously and return a validated GeneratedTestSuite.

    Args:
        story:     Normalized StoryContext — the only input Gemini should see.
        max_tests: Cap on the number of test cases to generate (default 5).

    Returns:
        A validated GeneratedTestSuite.

    Raises:
        ValueError: If Gemini's output cannot be parsed or validated.
    """
    settings = get_settings()
    client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())

    prompt = build_prompt(story, max_tests=max_tests)

    response = client.models.generate_content(
        model=_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.2,        # low temperature = more deterministic output
            max_output_tokens=8192,
        ),
    )

    raw_text = response.text
    if not raw_text:
        raise ValueError("Gemini returned an empty response.")
    return _parse_suite(raw_text, story.issue_key)
