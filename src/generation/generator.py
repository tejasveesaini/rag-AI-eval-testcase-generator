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

# Model to use — gemini-3.1-flash-lite-preview: fast, low-cost, and aligned
# with the evaluator so generation and judgment use the same model family.
# The API supports much larger output limits, but 8192 is a deliberate request
# cap that is sufficient for a 5-test JSON suite while keeping responses tight.
_MODEL = "gemini-3.1-flash-lite-preview"
_MODEL_TIMEOUT_MS = 120_000

# Strip markdown code fences if Gemini wraps output despite instructions
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```")
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
# Matches unquoted object keys: word chars at the start of a JSON "property" slot
_UNQUOTED_KEY_RE = re.compile(r'(?<=[{,])\s*([A-Za-z_]\w*)\s*:')
# Matches single-quoted strings (non-escaped apostrophes not inside double-quoted context)
_SINGLE_QUOTE_STR_RE = re.compile(r"'((?:[^'\\]|\\.)*)'")


def _extract_json_object(text: str) -> str:
    """Trim leading/trailing chatter and return the outermost JSON object text."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return text
    return text[start : end + 1]


def _strip_control_chars(text: str) -> str:
    """Remove ASCII control characters (except tab/newline/CR) that break JSON parsers."""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)


def _repair_json_like_text(text: str) -> str:
    """Apply small, safe repairs for common model formatting defects."""
    cleaned = text.strip().replace("\ufeff", "")
    cleaned = _extract_json_object(cleaned)
    cleaned = _TRAILING_COMMA_RE.sub(r"\1", cleaned)
    cleaned = _strip_control_chars(cleaned)
    return cleaned


def _fix_unescaped_quotes_in_strings(text: str) -> str:
    """Walk the JSON character by character and escape any bare double-quotes
    that appear *inside* a string value (i.e. not as structural delimiters).

    This is the most common cause of "Expecting property name enclosed in double
    quotes" errors: a string value like ``"say "hello""`` that the model forgot
    to escape.  A regex cannot distinguish structural from content quotes, but a
    state machine can.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    # Track whether the current position is at a structural context where a `"`
    # is expected to be an opening delimiter (i.e. we just saw `:`, `,`, `[`, `{`
    # optionally followed by whitespace).
    after_structural = True  # start of document is a structural position

    while i < n:
        ch = text[i]

        if ch == '\\' and in_string:
            # Escape sequence — copy both the backslash and the next char verbatim
            out.append(ch)
            i += 1
            if i < n:
                out.append(text[i])
                i += 1
            continue

        if ch == '"':
            if not in_string:
                if after_structural:
                    # This is a legitimate string-opening quote
                    in_string = True
                    after_structural = False
                    out.append(ch)
                else:
                    # Bare `"` at a non-structural position — treat as a content
                    # quote that should have been escaped
                    out.append('\\"')
            else:
                # We're in a string — this could be:
                #   a) the closing delimiter, or
                #   b) an unescaped content quote
                # Heuristic: peek ahead past whitespace; if the next non-ws
                # char is `:`, `,`, `}`, `]` or end-of-input, treat as closing.
                j = i + 1
                while j < n and text[j] in ' \t\r\n':
                    j += 1
                next_ch = text[j] if j < n else ''
                if next_ch in (':', ',', '}', ']', ''):
                    # Closing delimiter
                    in_string = False
                    after_structural = next_ch in (',', ':', '[', '{')
                    out.append(ch)
                else:
                    # Content quote — escape it
                    out.append('\\"')
            i += 1
            continue

        # Track structural positions outside strings
        if not in_string:
            if ch in (':', ',', '[', '{'):
                after_structural = True
            elif ch not in (' ', '\t', '\r', '\n'):
                after_structural = False

        out.append(ch)
        i += 1

    return ''.join(out)


def _repair_json_aggressively(text: str) -> str:
    """Heavier repairs: unquoted keys, single-quoted strings, control chars.

    Applied only when the lightweight repair + YAML fallback both fail.
    Each transform is order-sensitive:
      1. Strip control characters first (they confuse all later regexes).
      2. Fix unescaped double-quotes inside string values.
      3. Replace single-quoted strings with double-quoted equivalents.
      4. Quote unquoted object keys.
      5. Re-strip trailing commas (single-quote replacement can introduce them).
    """
    out = _strip_control_chars(text.strip().replace("\ufeff", ""))
    out = _extract_json_object(out)

    # Fix unescaped double-quotes inside string values first (most common cause
    # of "Expecting property name enclosed in double quotes" at mid-body positions)
    out = _fix_unescaped_quotes_in_strings(out)

    # Replace 'single quoted' → "double quoted" strings.
    # Only replace where we're clearly not inside an already-double-quoted string
    # (this regex is a best-effort heuristic, not a full parser).
    def _sq_to_dq(m: re.Match) -> str:
        inner = m.group(1).replace('"', '\\"')
        return f'"{inner}"'
    out = _SINGLE_QUOTE_STR_RE.sub(_sq_to_dq, out)

    # Quote unquoted object keys
    out = _UNQUOTED_KEY_RE.sub(lambda m: m.group(0).replace(m.group(1), f'"{m.group(1)}"'), out)

    # Final trailing-comma pass
    out = _TRAILING_COMMA_RE.sub(r"\1", out)
    return out


def _load_json_like(text: str) -> dict:
    """Parse strict JSON first, then escalate through progressively heavier repairs."""
    # ── Pass 1: strict JSON ───────────────────────────────────────────────────
    try:
        return json.loads(text)
    except json.JSONDecodeError as json_error:
        pass

    # ── Pass 2: lightweight repair (trim noise, trailing commas, BOM) ────────
    repaired = _repair_json_like_text(text)
    try:
        logger.warning("Gemini returned malformed JSON; applying lightweight repair.")
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass

    # ── Pass 3: YAML (handles unquoted keys, single quotes, comments) ────────
    try:
        import yaml
        loaded = yaml.safe_load(repaired)
        if isinstance(loaded, dict):
            logger.warning("Gemini returned malformed JSON; recovered via YAML parser.")
            return loaded
    except Exception:
        pass

    # ── Pass 4: fix unescaped double-quotes inside string values ─────────────
    quote_fixed = _fix_unescaped_quotes_in_strings(repaired)
    if quote_fixed != repaired:
        try:
            logger.warning("Gemini returned malformed JSON; fixed unescaped quotes in strings.")
            return json.loads(quote_fixed)
        except json.JSONDecodeError:
            pass

    # ── Pass 5: aggressive char-level repair ─────────────────────────────────
    aggressively_repaired = _repair_json_aggressively(text)
    try:
        logger.warning("Gemini returned malformed JSON; applying aggressive repair.")
        return json.loads(aggressively_repaired)
    except json.JSONDecodeError:
        pass

    # ── Pass 6: YAML on aggressive repair ────────────────────────────────────
    try:
        import yaml
        loaded = yaml.safe_load(aggressively_repaired)
        if isinstance(loaded, dict):
            logger.warning("Gemini returned malformed JSON; recovered via YAML on aggressively-repaired text.")
            return loaded
    except Exception:
        pass

    # All passes exhausted — raise the original error
    raise json.JSONDecodeError(
        "All JSON repair passes failed",
        text,
        0,
    )


def _recover_truncated_tests(text: str) -> dict | None:
    """Last-resort recovery for truncated JSON: extract all complete test objects.

    When the model hits the output token limit mid-string the closing braces are
    missing.  This function finds every fully-closed ``{...}`` block inside the
    ``"tests"`` array and rebuilds a minimal valid JSON document from them so the
    caller gets *some* tests rather than a hard failure.

    Returns a dict on success, or None if no complete test could be extracted.
    """
    # Locate the opening of the tests array
    tests_start = text.find('"tests"')
    if tests_start == -1:
        return None
    bracket = text.find("[", tests_start)
    if bracket == -1:
        return None

    # Walk the array character by character, collecting complete objects
    complete_objects: list[str] = []
    depth = 0
    obj_start: int | None = None
    i = bracket + 1
    in_string = False
    escape_next = False

    while i < len(text):
        ch = text[i]
        if escape_next:
            escape_next = False
            i += 1
            continue
        if ch == "\\" and in_string:
            escape_next = True
            i += 1
            continue
        if ch == '"':
            in_string = not in_string
        if not in_string:
            if ch == "{":
                if depth == 0:
                    obj_start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and obj_start is not None:
                    complete_objects.append(text[obj_start : i + 1])
                    obj_start = None
        i += 1

    if not complete_objects:
        return None

    # Rebuild a minimal valid JSON document
    story_key_match = re.search(r'"story_key"\s*:\s*"([^"]+)"', text)
    story_key = story_key_match.group(1) if story_key_match else "UNKNOWN"
    joined = ",\n".join(complete_objects)
    recovered = f'{{"story_key": "{story_key}", "tests": [{joined}], "notes": "⚠ Output was truncated; {len(complete_objects)} complete test(s) recovered."}}'
    try:
        return json.loads(recovered)
    except json.JSONDecodeError:
        return None


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
        # Attempt to salvage complete test objects from a truncated response
        recovered = _recover_truncated_tests(text)
        if recovered:
            logger.warning(
                "Gemini output was truncated; recovered %d complete test(s) from partial JSON.",
                len(recovered.get("tests", [])),
            )
            data = recovered
        else:
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
    max_tests: int = 5,
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
                temperature=0.2,        # low temperature = more deterministic output
                max_output_tokens=8192, # deliberate request cap; keeps JSON output bounded
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
