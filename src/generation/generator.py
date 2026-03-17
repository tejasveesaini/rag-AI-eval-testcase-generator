"""Gemini-based test-case generator.

Single responsibility: send a prompt to Gemini, parse the JSON response,
and return a validated GeneratedTestSuite.

Nothing in here touches Jira or the API layer.

Two generation modes:
  baseline  — generate_test_suite(story)
  enriched  — generate_test_suite(story, context=package)

Discovery-enriched mode
-----------------------
When ``run_discovery=True``, generate_test_suite() runs the retrieval
discovery pipeline before calling Gemini:
  1. Search Jira for related issues via keyword + fallback JQL.
  2. Normalize and index them in ChromaDB.
  3. Retrieve top-N most similar documents.
  4. Merge retrieved ContextItems into the ContextPackage (or build one).
This is the recommended mode for best generation quality.

Pre-generation input guard
--------------------------
Before calling Gemini, generate_test_suite() runs check_input() from
src/evaluation/input_guard.py.

  BLOCK verdict  → raises InputRejectedError immediately (no LLM call).
  WARN  verdict  → proceeds but attaches guard warnings to the suite notes.
  PASS  verdict  → proceeds normally.

Callers that want to inspect the guard result without catching exceptions
can call check_input() directly before calling generate_test_suite().
"""

import json
import logging
import re

from google import genai
from google.genai import types

from src.config import get_settings
from src.evaluation.input_guard import InputRejectedError, check_input
from src.generation.prompt import build_prompt
from src.models.schemas import (
    ContextItem,
    ContextItemType,
    ContextPackage,
    GeneratedTestSuite,
    StoryContext,
    Verdict,
)

logger = logging.getLogger(__name__)

# Model to use — confirmed available via API
_MODEL = "gemini-3-flash-preview"
_MODEL_TIMEOUT_MS = 120_000

# Strip markdown code fences if Gemini wraps output despite instructions
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```")
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _extract_json_object(text: str) -> str:
    """Trim leading/trailing chatter and return the outermost JSON object text."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return text
    return text[start : end + 1]


def _repair_json_like_text(text: str) -> str:
    """Apply small, safe repairs for common model formatting defects."""
    cleaned = text.strip().replace("\ufeff", "")
    cleaned = _extract_json_object(cleaned)
    cleaned = _TRAILING_COMMA_RE.sub(r"\1", cleaned)
    return cleaned


def _load_json_like(text: str) -> dict:
    """Parse strict JSON first, then fall back to YAML for near-JSON output."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as json_error:
        repaired = _repair_json_like_text(text)
        if repaired != text:
            try:
                logger.warning("Gemini returned malformed JSON; applying lightweight repair before parsing.")
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass

        try:
            import yaml
        except ModuleNotFoundError as exc:
            raise json_error from exc

        try:
            loaded = yaml.safe_load(repaired)
        except Exception as yaml_error:
            raise json_error from yaml_error

        if not isinstance(loaded, dict):
            raise json_error
        logger.warning("Gemini returned malformed JSON; recovered by parsing repaired output as YAML.")
        return loaded


def _parse_suite(raw_text: str, issue_key: str) -> GeneratedTestSuite:
    """Extract and validate a GeneratedTestSuite from Gemini's raw text output."""
    text = raw_text.strip()

    # Strip code fences if present
    fence_match = _CODE_FENCE_RE.search(text)
    if fence_match:
        text = fence_match.group(1).strip()

    try:
        data = _load_json_like(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Gemini returned non-JSON output.\n"
            f"Parse error: {e}\n"
            f"Raw output (first 500 chars):\n{raw_text[:500]}"
        )
    except Exception as e:
        raise ValueError(
            f"Gemini returned malformed structured output that could not be repaired.\n"
            f"Parse error: {e}\n"
            f"Raw output (first 500 chars):\n{raw_text[:500]}"
        )

    # Ensure source_story is set on every test case (model may omit it)
    for tc in data.get("tests", []):
        tc.setdefault("source_story", issue_key)

    return GeneratedTestSuite.model_validate(data)


def _query_results_to_context_items(query_results) -> list[ContextItem]:
    """Convert QueryResult objects from the vector store into ContextItems.

    Maps source_type → ContextItemType for prompt sectioning:
      bug              → BUG
      historical_test  → TEST
      story / qa_note  → STORY
    """
    from src.models.schemas import SourceType

    _type_map = {
        SourceType.BUG:             ContextItemType.BUG,
        SourceType.HISTORICAL_TEST: ContextItemType.TEST,
        SourceType.STORY:           ContextItemType.STORY,
        SourceType.QA_NOTE:         ContextItemType.STORY,
    }

    items: list[ContextItem] = []
    for r in query_results:
        cat = _type_map.get(r.source_type, ContextItemType.OTHER)
        items.append(ContextItem(
            key=r.source_key,
            issue_type=r.source_type.value,
            category=cat,
            summary=r.title,
            short_text=r.body if r.body != r.title else None,
            relevance_hint=f"retrieved (score={r.score:.2f})",
        ))
    return items


def _merge_discovery_into_package(
    existing: ContextPackage | None,
    story_key: str,
    discovered_items: list[ContextItem],
    retrieved_items: list[ContextItem],
) -> ContextPackage:
    """Merge discovered + retrieved items into a ContextPackage.

    Priority: existing package items are kept first; new items are appended
    without duplication (by key). Retrieved items (from similarity search)
    come before discovered items (raw JQL finds) so the most relevant
    historical context leads the prompt sections.

    Args:
        existing:         Existing ContextPackage, or None for baseline mode.
        story_key:        The story key (required when building from scratch).
        discovered_items: Items from keyword/fallback Jira search.
        retrieved_items:  Items from ChromaDB similarity search.

    Returns:
        Enriched ContextPackage.
    """
    if existing is None:
        base = ContextPackage(story_key=story_key)
    else:
        base = existing

    # Collect all currently known keys to avoid duplication
    known_keys: set[str] = set()
    for item in (
        base.linked_defects + base.historical_tests + base.related_stories
    ):
        known_keys.add(item.key)

    # Merge retrieved items first (highest signal)
    new_defects: list[ContextItem] = list(base.linked_defects)
    new_tests:   list[ContextItem] = list(base.historical_tests)
    new_stories: list[ContextItem] = list(base.related_stories)

    for item in retrieved_items + discovered_items:
        if item.key in known_keys or item.key == story_key:
            continue
        known_keys.add(item.key)
        if item.category == ContextItemType.BUG:
            new_defects.append(item)
        elif item.category == ContextItemType.TEST:
            new_tests.append(item)
        else:
            new_stories.append(item)

    return ContextPackage(
        story_key=story_key,
        linked_defects=new_defects,
        historical_tests=new_tests,
        related_stories=new_stories,
        coverage_hints=base.coverage_hints,
    )


def generate_test_suite(
    story: StoryContext,
    max_tests: int = 10,
    context: ContextPackage | None = None,
    run_discovery: bool = False,
    excluded_titles: list[str] | None = None,
) -> GeneratedTestSuite:
    """Call Gemini synchronously and return a validated GeneratedTestSuite.

    Discovery (run_discovery=True):
      Before building the prompt, runs the full Jira keyword search +
      ChromaDB retrieval pipeline to find relevant historical issues:
        1. Keyword JQL → raw Jira issues → ContextItems.
        2. Normalize + index into ChromaDB.
        3. Similarity search → top-N QueryResults → ContextItems.
        4. Merge everything into context (or create a new ContextPackage).
      This gives the prompt builder rich historical context even when no
      ContextPackage was pre-computed.

    Pre-generation input guard:
      - BLOCK → raises InputRejectedError (no LLM call is made).
      - WARN  → proceeds; guard warnings are appended to suite.notes.
      - PASS  → proceeds normally.

    Args:
        story:          Normalized StoryContext — the primary source of truth.
        max_tests:      Cap on the number of test cases to generate (default 5).
        context:        Optional pre-computed ContextPackage.
                        When None, runs in baseline mode (story only) unless
                        run_discovery=True, in which case discovery builds one.
        run_discovery:  If True, run the keyword JQL + ChromaDB discovery
                        pipeline before calling Gemini. Default False to
                        preserve backward compatibility.

    Returns:
        A validated GeneratedTestSuite.

    Raises:
        InputRejectedError: If the input guard returns BLOCK.
        ValueError: If Gemini's output cannot be parsed or validated.
    """
    # ── Discovery: find + index related issues, retrieve top-N ───────────────
    if run_discovery:
        try:
            from src.retrieval.discovery import discover_and_index_sync  # lazy import

            logger.info("Running discovery pipeline for %s …", story.issue_key)
            discovery = discover_and_index_sync(story, n_results=5)

            # Convert QueryResults → ContextItems
            retrieved_items = _query_results_to_context_items(
                discovery.top_results()
            )

            # Merge into (or build) the ContextPackage
            context = _merge_discovery_into_package(
                existing=context,
                story_key=story.issue_key,
                discovered_items=discovery.context_items,
                retrieved_items=retrieved_items,
            )
            logger.info(
                "Discovery enriched context: defects=%d tests=%d stories=%d",
                len(context.linked_defects),
                len(context.historical_tests),
                len(context.related_stories),
            )
        except Exception as exc:
            # Discovery failure must never block generation
            logger.warning(
                "Discovery pipeline failed for %s: %s — falling back to provided context.",
                story.issue_key,
                exc,
            )

    mode = "enriched" if context is not None else "baseline"

    # ── Pre-generation input quality guard ────────────────────────────────────
    guard_report = check_input(story, context=context, mode=mode)

    if guard_report.verdict == Verdict.BLOCK:
        raise InputRejectedError(guard_report)

    # Collect warning messages to attach to the suite notes after generation
    guard_warnings: list[str] = []
    if guard_report.verdict == Verdict.WARN:
        for r in guard_report.signal_results:
            if r.verdict == Verdict.WARN:
                guard_warnings.append(f"[input-guard:{r.signal.value}] {r.detail}")

    # ── Call Gemini ───────────────────────────────────────────────────────────
    settings = get_settings()
    client = genai.Client(
        api_key=settings.gemini_api_key.get_secret_value(),
        http_options=types.HttpOptions(timeout=_MODEL_TIMEOUT_MS),
    )

    prompt = build_prompt(story, max_tests=max_tests, context=context, excluded_titles=excluded_titles or [])

    try:
        response = client.models.generate_content(
            model=_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,         # low temperature = more deterministic output
                max_output_tokens=16384, # thinking tokens consume ~8k; need 16k headroom
            ),
        )
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        lowered = message.lower()
        if "timed out" in lowered or "timeout" in lowered:
            raise TimeoutError(
                f"Gemini generation timed out after {_MODEL_TIMEOUT_MS // 1000} seconds."
            ) from exc
        raise RuntimeError(f"Gemini generation failed: {message}") from exc

    raw_text = response.text
    if not raw_text:
        raise ValueError("Gemini returned an empty response.")

    suite = _parse_suite(raw_text, story.issue_key)

    # Attach guard warnings to suite notes so they surface in the output
    if guard_warnings:
        warning_block = "INPUT WARNINGS:\n" + "\n".join(f"  • {w}" for w in guard_warnings)
        if suite.notes:
            suite = suite.model_copy(update={"notes": suite.notes + "\n\n" + warning_block})
        else:
            suite = suite.model_copy(update={"notes": warning_block})

    return suite
