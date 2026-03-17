from __future__ import annotations

import asyncio
import json
import sys
import traceback
from collections import Counter
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
NORMALIZED_DIR = DATA_DIR / "normalized"
CONTEXT_DIR = DATA_DIR / "context"
GENERATED_DIR = DATA_DIR / "generated"
RAW_STORY_DIR = DATA_DIR / "sample_stories"
PUSH_HISTORY_FILE = DATA_DIR / "push_history.json"
APP_LOGS: deque[dict[str, str]] = deque(maxlen=400)


def _is_test_like_issue_type(issue_type: str | None) -> bool:
    normalized = "".join(ch for ch in (issue_type or "").lower() if ch.isalnum())
    return "test" in normalized or normalized == "subtask"


def _normalize_title_key(value: str | None) -> str:
    normalized = "".join(ch.lower() if ch.isalnum() else " " for ch in (value or ""))
    return " ".join(normalized.split())


def _title_tokens(value: str | None) -> set[str]:
    return {token for token in _normalize_title_key(value).split() if len(token) > 2}


def _jaccard_similarity(left: set[str], right: set[str]) -> float:
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _is_covered_title(
    title: str | None,
    exact_keys: set[str],
    token_sets: list[set[str]],
    threshold: float = 0.55,
) -> bool:
    normalized = _normalize_title_key(title)
    if not normalized:
        return False
    if normalized in exact_keys:
        return True
    tokens = _title_tokens(title)
    return any(_jaccard_similarity(tokens, existing) >= threshold for existing in token_sets)


def _merge_notes(*chunks: str) -> str:
    merged: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        cleaned = (chunk or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        merged.append(cleaned)
    return "\n\n".join(merged)


@dataclass
class ActionResult:
    ok: bool
    message: str
    payload: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def add_app_log(message: str, level: str = "info") -> None:
    APP_LOGS.appendleft(
        {
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "message": message,
        }
    )


def add_app_data_log(label: str, payload: Any, level: str = "info", limit: int = 5000) -> None:
    try:
        formatted = json.dumps(payload, indent=2, ensure_ascii=False)
    except TypeError:
        formatted = repr(payload)
    if len(formatted) > limit:
        formatted = f"{formatted[:limit]}…"
    add_app_log(f"{label}\n{formatted}", level=level)


def get_app_logs(limit: int = 200) -> dict[str, Any]:
    return {"logs": list(APP_LOGS)[:limit]}


def _clear_generated_suites(issue_key: str) -> int:
    removed = 0
    for path in (
        GENERATED_DIR / f"{issue_key}_baseline.json",
        GENERATED_DIR / f"{issue_key}_enriched.json",
    ):
        if path.exists():
            path.unlink()
            removed += 1
    return removed


def discover_workspace() -> dict[str, Any]:
    issues = _build_issue_index()
    counts = {
        "issues": len(issues),
        "normalized": sum(1 for issue in issues if issue["normalized"]),
        "context": sum(1 for issue in issues if issue["context"]),
        "baseline": sum(1 for issue in issues if issue["baseline"]),
        "enriched": sum(1 for issue in issues if issue["enriched"]),
    }
    return {
        "root": str(ROOT),
        "env_present": (ROOT / ".env").exists(),
        "counts": counts,
        "issues": issues,
    }


def get_issue_bundle(issue_key: str) -> dict[str, Any]:
    key = _normalize_issue_key(issue_key)
    files = {
        "raw": RAW_STORY_DIR / f"{key}.json",
        "normalized": NORMALIZED_DIR / f"{key}.json",
        "context": CONTEXT_DIR / f"{key}.json",
        "baseline": GENERATED_DIR / f"{key}_baseline.json",
        "enriched": GENERATED_DIR / f"{key}_enriched.json",
    }
    story = _load_json(files["normalized"])
    context = _load_json(files["context"])
    baseline_suite = _load_json(files["baseline"])
    enriched_suite = _load_json(files["enriched"])
    raw_summary = _extract_raw_summary(_load_json(files["raw"]))

    return {
        "issue_key": key,
        "summary": (story or {}).get("summary") or raw_summary,
        "files": {name: path.exists() for name, path in files.items()},
        "story": story,
        "context": context,
        "suites": {
            "baseline": _summarize_suite(baseline_suite, files["baseline"]),
            "enriched": _summarize_suite(enriched_suite, files["enriched"]),
        },
    }


def fetch_story(issue_key: str) -> ActionResult:
    key = _normalize_issue_key(issue_key)
    add_app_log(f"Fetch requested for {key}.")
    try:
        story = asyncio.run(_fetch_story_async(key))
        removed_suites = _clear_generated_suites(key)
        if removed_suites:
            add_app_log(
                f"Cleared {removed_suites} previously generated suite file(s) for {key}.",
                level="info",
            )
        add_app_log(f"Fetch completed for {key}. Raw and normalized story files were updated.", level="success")
        add_app_data_log(
            f"Story received for {key}:",
            {
                "issue_key": story.get("issue_key"),
                "summary": story.get("summary"),
                "description": story.get("description"),
                "acceptance_criteria": story.get("acceptance_criteria"),
                "labels": story.get("labels"),
                "components": story.get("components"),
                "linked_issues": story.get("linked_issues"),
            },
        )
        payload = {
            "workspace": discover_workspace(),
            "issue": get_issue_bundle(key),
            "story": story,
            "logs": get_app_logs(),
        }
        return ActionResult(
            ok=True,
            message=f"Fetched {key} from Jira and saved raw plus normalized story data.",
            payload=payload,
        )
    except Exception as exc:  # pragma: no cover - surfaced to the UI
        return _action_error(f"Unable to fetch {key}.", exc)


def collect_context(issue_key: str) -> ActionResult:
    key = _normalize_issue_key(issue_key)
    add_app_log(f"Context collection requested for {key}.")
    try:
        package = asyncio.run(_collect_context_async(key))
        add_app_log(
            f"Context collection completed for {key}. Loaded {len(package.get('linked_defects', []))} linked defects, "
            f"{len(package.get('historical_tests', []))} historical tests, and {len(package.get('related_stories', []))} related stories.",
            level="success",
        )
        add_app_data_log(
            f"Context received for {key}:",
            {
                "story_key": package.get("story_key"),
                "linked_defects": package.get("linked_defects"),
                "historical_tests": package.get("historical_tests"),
                "related_stories": package.get("related_stories"),
                "coverage_hints": package.get("coverage_hints"),
            },
        )
        payload = {
            "workspace": discover_workspace(),
            "issue": get_issue_bundle(key),
            "context": package,
            "logs": get_app_logs(),
        }
        return ActionResult(
            ok=True,
            message=f"Collected historical context for {key}.",
            payload=payload,
        )
    except Exception as exc:  # pragma: no cover - surfaced to the UI
        return _action_error(f"Unable to collect context for {key}.", exc)


def generate_suite(issue_key: str, mode: str, max_tests: int = 10, offset: int = 0) -> ActionResult:
    key = _normalize_issue_key(issue_key)
    ui_mode = _normalize_mode(mode)
    batch_label = f"next {max_tests}" if offset > 0 else f"first {max_tests}"
    add_app_log(f"{ui_mode.title()} generation requested for {key} ({batch_label} tests, offset={offset}).")
    story_path = NORMALIZED_DIR / f"{key}.json"
    if not story_path.exists():
        return ActionResult(
            ok=False,
            message=f"No normalized story found for {key}. Fetch the story first.",
        )

    try:
        # ── Pre-generation input guard (only on the first batch) ───────────
        if offset == 0:
            guard_result = _run_input_guard(key, ui_mode, story_path)
            if guard_result is not None:
                return guard_result

        add_app_log(
            f"Starting {ui_mode} generation for {key} (offset={offset}, max={max_tests}). Calling Gemini.",
            level="info",
        )
        suite = _generate_suite(key, ui_mode, story_path, max_tests=max_tests, offset=offset)
        suite_path = GENERATED_DIR / f"{key}_{ui_mode}.json"
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        suite_path.write_text(json.dumps(suite, indent=2))
        add_app_log(
            f"{ui_mode.title()} generation completed for {key}. Suite now has {len(suite.get('tests', []))} test cases.",
            level="success",
        )
        add_app_data_log(
            f"Generated suite for {key} ({ui_mode}):",
            {
                "story_key": suite.get("story_key"),
                "tests": suite.get("tests"),
                "notes": suite.get("notes"),
            },
        )
        payload = {
            "workspace": discover_workspace(),
            "issue": get_issue_bundle(key),
            "suite": suite,
            "mode": ui_mode,
            "evaluation": evaluate_suite_data(suite_path),
            "logs": get_app_logs(),
        }
        return ActionResult(
            ok=True,
            message=f"Generated {ui_mode} suite for {key}: {len(suite.get('tests', []))} total tests.",
            payload=payload,
        )
    except Exception as exc:  # pragma: no cover - surfaced to the UI
        return _action_error(f"Unable to generate the {ui_mode} suite for {key}.", exc)


def evaluate_suite(issue_key: str, mode: str) -> ActionResult:
    key = _normalize_issue_key(issue_key)
    ui_mode = _normalize_mode(mode)
    add_app_log(f"Evaluation requested for {key} ({ui_mode}).")
    suite_path = GENERATED_DIR / f"{key}_{ui_mode}.json"
    if not suite_path.exists():
        return ActionResult(
            ok=False,
            message=f"No {ui_mode} suite found for {key}. Generate it first.",
        )

    structural = evaluate_suite_data(suite_path)
    deepeval = _run_deepeval_detailed(key, ui_mode)
    overall_passed = deepeval["passed"]
    evaluation = {
        "mode": ui_mode,
        "passed": overall_passed,
        "structural": structural,
        "deepeval": deepeval,
    }
    status = "passed" if overall_passed else "failed"
    add_app_log(f"Evaluation {status} for {key} ({ui_mode}).", level="success" if overall_passed else "warning")
    add_app_data_log(
        f"Evaluation details for {key} ({ui_mode}):",
        evaluation,
    )
    payload = {
        "workspace": discover_workspace(),
        "issue": get_issue_bundle(key),
        "mode": ui_mode,
        "evaluation": evaluation,
        "logs": get_app_logs(),
    }
    return ActionResult(
        ok=True,
        message=f"{key} {ui_mode} evaluation {status}.",
        payload=payload,
    )


def evaluate_suite_data(path: Path) -> dict[str, Any]:
    data = _load_json(path)
    if not isinstance(data, dict):
        return {
            "path": str(path),
            "passed": False,
            "results": [
                {
                    "name": "valid_json",
                    "passed": False,
                    "detail": "File could not be parsed as a JSON object.",
                }
            ],
        }

    results: list[dict[str, Any]] = []
    tests = data.get("tests")
    story_key = (data.get("story_key") or "").strip()

    results.append(
        {
            "name": "schema_shape",
            "passed": isinstance(tests, list) and bool(story_key),
            "detail": "" if isinstance(tests, list) and bool(story_key) else "Missing story_key or tests array.",
        }
    )

    valid_priorities = {"High", "Medium", "Low"}
    valid_types = {"Functional", "Edge Case", "Negative", "Integration"}

    if not isinstance(tests, list):
        return {"path": str(path), "passed": False, "results": results}

    has_negative = False
    gate_passed = True

    for index, test in enumerate(tests):
        title = str(test.get("title", "")).strip()
        expected = str(test.get("expected_result", "")).strip()
        source_story = str(test.get("source_story", "")).strip()
        steps = test.get("steps")
        priority = str(test.get("priority", ""))
        test_type = str(test.get("test_type", ""))

        if not title or not expected or not source_story or not isinstance(steps, list) or not steps:
            gate_passed = False

        if test_type == "Negative":
            has_negative = True

        results.extend(
            [
                {
                    "name": f"test[{index}].required_fields",
                    "passed": bool(title and expected and source_story),
                    "detail": "" if title and expected and source_story else "Title, expected_result, and source_story must be non-empty.",
                },
                {
                    "name": f"test[{index}].steps",
                    "passed": isinstance(steps, list) and bool(steps) and all(str(step).strip() for step in steps),
                    "detail": "" if isinstance(steps, list) and bool(steps) and all(str(step).strip() for step in steps) else "Each test needs at least one non-blank step.",
                },
                {
                    "name": f"test[{index}].priority",
                    "passed": priority in valid_priorities,
                    "detail": "" if priority in valid_priorities else f"Priority must be one of {sorted(valid_priorities)}.",
                },
                {
                    "name": f"test[{index}].test_type",
                    "passed": test_type in valid_types,
                    "detail": "" if test_type in valid_types else f"test_type must be one of {sorted(valid_types)}.",
                },
                {
                    "name": f"test[{index}].source_story",
                    "passed": source_story == story_key,
                    "detail": "" if source_story == story_key else f"Expected source_story={story_key!r}, got {source_story!r}.",
                },
            ]
        )

    results.append(
        {
            "name": "has_negative_test",
            "passed": has_negative,
            "detail": "" if has_negative else "No test with test_type='Negative' found.",
        }
    )
    results.append(
        {
            "name": "inline_gate",
            "passed": gate_passed,
            "detail": "" if gate_passed else "One or more tests failed the lightweight structural gate.",
        }
    )

    return {
        "path": str(path),
        "passed": all(result["passed"] for result in results),
        "results": results,
    }


async def _fetch_story_async(issue_key: str) -> dict[str, Any]:
    try:
        from src.jira.client import JiraClient
        from src.jira.ingestor import parse_issue
    except ModuleNotFoundError as exc:  # pragma: no cover - import error depends on user env
        raise RuntimeError(_dependency_help(exc)) from exc

    RAW_STORY_DIR.mkdir(parents=True, exist_ok=True)
    NORMALIZED_DIR.mkdir(parents=True, exist_ok=True)

    client = JiraClient()
    raw = await client.get_issue(issue_key)
    story = parse_issue(raw)
    add_app_log(f"Fetched {issue_key} from Jira with summary: {story.summary}", level="info")
    add_app_data_log(
        f"Jira response payload for {issue_key}:",
        raw,
        limit=12000,
    )

    raw_path = RAW_STORY_DIR / f"{issue_key}.json"
    normalized_path = NORMALIZED_DIR / f"{issue_key}.json"
    raw_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False))
    normalized_path.write_text(story.model_dump_json(indent=2))
    add_app_log(
        f"Saved {raw_path.relative_to(ROOT)} and {normalized_path.relative_to(ROOT)}.",
        level="info",
    )
    normalized_story = story.model_dump(mode="json")
    add_app_data_log(
        f"Stored raw story file for {issue_key} ({raw_path.relative_to(ROOT)}):",
        raw,
        limit=12000,
    )
    add_app_data_log(
        f"Stored normalized story file for {issue_key} ({normalized_path.relative_to(ROOT)}):",
        normalized_story,
        limit=12000,
    )
    return normalized_story


async def _collect_context_async(issue_key: str) -> dict[str, Any]:
    try:
        from src.context.collector import collect_raw_context
        from src.context.normalizer import normalize_raw_context
        from src.context.packager import build_context_package, save_context_package
        from src.jira.client import JiraClient
        from src.jira.ingestor import parse_issue
    except ModuleNotFoundError as exc:  # pragma: no cover - import error depends on user env
        raise RuntimeError(_dependency_help(exc)) from exc

    jira = JiraClient()
    raw_story = await jira.get_issue(issue_key)
    story = parse_issue(raw_story)
    raw_context = await collect_raw_context(issue_key)
    add_app_data_log(
        f"Raw collected context for {issue_key}:",
        raw_context,
        limit=12000,
    )
    normalized = normalize_raw_context(raw_context)
    package = build_context_package(story, normalized["linked"], normalized["jql"])
    save_context_package(package)
    add_app_log(
        f"Packaged context for {issue_key}: {len(package.linked_defects)} linked defects, "
        f"{len(package.historical_tests)} historical tests, {len(package.related_stories)} related stories.",
        level="info",
    )
    packaged_context = package.model_dump(mode="json")
    context_path = CONTEXT_DIR / f"{issue_key}.json"
    add_app_data_log(
        f"Stored context package for {issue_key} ({context_path.relative_to(ROOT)}):",
        packaged_context,
        limit=12000,
    )
    return packaged_context


def _run_input_guard(issue_key: str, mode: str, story_path: Path) -> ActionResult | None:
    """Run the pre-generation input quality guard.

    Returns an ActionResult(ok=False) if generation should be BLOCKED,
    an ActionResult(ok=True, ...) with a warning payload if WARN (caller
    can still proceed but the warning is surfaced), or None if PASS.

    None means "proceed normally — guard passed".
    """
    try:
        from src.evaluation.input_guard import check_input
        from src.models.schemas import ContextPackage, StoryContext, Verdict
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(_dependency_help(exc)) from exc

    story = StoryContext.model_validate_json(story_path.read_text())
    context: ContextPackage | None = None
    if mode == "enriched":
        context_path = CONTEXT_DIR / f"{issue_key}.json"
        if context_path.exists():
            context = ContextPackage.model_validate_json(context_path.read_text())

    report = check_input(story, context=context, mode=mode)  # type: ignore[arg-type]

    if report.verdict == Verdict.BLOCK:
        # Serialize signal details for the UI
        signals = [
            {"signal": r.signal.value, "verdict": r.verdict.value, "detail": r.detail}
            for r in report.signal_results
            if r.verdict != Verdict.PASS
        ]
        return ActionResult(
            ok=False,
            message=f"Generation rejected for {issue_key}: {report.summary}",
            payload={
                "guard_verdict": report.verdict.value,
                "signals": signals,
                "workspace": discover_workspace(),
                "issue": get_issue_bundle(issue_key),
            },
        )

    if report.verdict == Verdict.WARN:
        # Warnings are attached to the payload after generation — return None
        # to allow generation to continue.  The generator itself will embed
        # warnings in suite.notes (via the guard wired into generator.py).
        pass

    return None  # PASS or WARN → proceed


def _generate_suite(issue_key: str, mode: str, story_path: Path, max_tests: int = 10, offset: int = 0) -> dict[str, Any]:
    try:
        from src.generation.generator import generate_test_suite
        from src.models.schemas import ContextPackage, StoryContext
    except ModuleNotFoundError as exc:  # pragma: no cover - import error depends on user env
        raise RuntimeError(_dependency_help(exc)) from exc

    story = StoryContext.model_validate_json(story_path.read_text())
    context = None

    if mode == "enriched":
        context_path = CONTEXT_DIR / f"{issue_key}.json"
        if not context_path.exists():
            raise RuntimeError(f"No context package found for {issue_key}. Collect context first.")
        context = ContextPackage.model_validate_json(context_path.read_text())
        add_app_log(f"Loaded enriched context from {context_path.relative_to(ROOT)}.", level="info")

    # ── Load existing tests when appending a next batch ───────────────────────
    existing_tests: list[dict] = []
    existing_notes: str = ""
    if offset > 0:
        suite_path = GENERATED_DIR / f"{issue_key}_{mode}.json"
        if suite_path.exists():
            existing_data = json.loads(suite_path.read_text())
            existing_tests = existing_data.get("tests", [])
            existing_notes = existing_data.get("notes", "")
            add_app_log(
                f"Appending to existing suite for {issue_key} ({mode}): {len(existing_tests)} tests already present.",
                level="info",
            )

    # Titles of already-generated tests and already-existing story tests
    # are passed to the model as exclusions so it targets new coverage.
    excluded_titles: list[str] = [t.get("title", "") for t in existing_tests if t.get("title")]
    excluded_titles.extend(
        item.summary
        for item in story.linked_issues
        if item.summary and _is_test_like_issue_type(item.issue_type)
    )
    if context is not None:
        excluded_titles.extend(item.summary for item in context.historical_tests if item.summary)

    # Deduplicate while preserving order.
    excluded_titles = list(dict.fromkeys(title.strip() for title in excluded_titles if title and title.strip()))
    excluded_exact_keys = {_normalize_title_key(title) for title in excluded_titles}
    excluded_token_sets = [_title_tokens(title) for title in excluded_titles]
    if excluded_titles:
        add_app_log(
            f"Excluding {len(excluded_titles)} existing test title(s) from generation for {issue_key} ({mode}).",
            level="info",
        )

    accepted_tests: list[dict[str, Any]] = []
    accepted_exact_keys: set[str] = set()
    accepted_token_sets: list[set[str]] = []
    note_chunks: list[str] = [existing_notes] if existing_notes else []
    max_attempts = 3

    for attempt in range(1, max_attempts + 1):
        remaining = max_tests - len(accepted_tests)
        if remaining <= 0:
            break

        attempt_excluded_titles = excluded_titles + [str(test.get("title", "")) for test in accepted_tests]
        new_suite = generate_test_suite(
            story,
            max_tests=remaining,
            context=context,
            excluded_titles=attempt_excluded_titles if attempt_excluded_titles else None,
        )
        note_chunks.append(new_suite.notes or "")
        add_app_log(
            f"Generator attempt {attempt}/{max_attempts} returned {len(new_suite.tests)} candidate tests for {issue_key} ({mode}).",
            level="info",
        )

        accepted_this_round = 0
        skipped_this_round = 0
        for test in new_suite.tests:
            if len(accepted_tests) >= max_tests:
                break

            if _is_covered_title(test.title, excluded_exact_keys, excluded_token_sets) or _is_covered_title(
                test.title,
                accepted_exact_keys,
                accepted_token_sets,
            ):
                skipped_this_round += 1
                continue

            accepted_tests.append(test.model_dump(mode="json"))
            accepted_exact_keys.add(_normalize_title_key(test.title))
            accepted_token_sets.append(_title_tokens(test.title))
            accepted_this_round += 1

        if skipped_this_round:
            add_app_log(
                f"Skipped {skipped_this_round} duplicate or near-duplicate test title(s) for {issue_key} ({mode}) on attempt {attempt}.",
                level="warning",
            )

        if accepted_this_round == 0 and len(accepted_tests) < max_tests:
            add_app_log(
                f"Attempt {attempt} did not add any new distinct tests for {issue_key} ({mode}).",
                level="warning",
            )

    if len(accepted_tests) < max_tests:
        add_app_log(
            f"Generation produced {len(accepted_tests)} distinct tests for {issue_key} ({mode}); requested {max_tests}.",
            level="warning",
        )
        note_chunks.append(
            f"Generated {len(accepted_tests)} distinct test case(s) after filtering duplicates; requested {max_tests}."
        )

    # ── Merge new tests with existing ones ────────────────────────────────────
    merged_tests = existing_tests + accepted_tests
    merged_notes = _merge_notes(*note_chunks)

    return {
        "story_key": issue_key,
        "tests": merged_tests,
        "notes": merged_notes,
    }


def _run_deepeval_detailed(issue_key: str, mode: str) -> dict[str, Any]:
    try:
        from deepeval.metrics import AnswerRelevancyMetric, ContextualRelevancyMetric, FaithfulnessMetric
        from deepeval.test_case import LLMTestCase
        from scripts.run_deepeval import (
            FAITHFULNESS_THRESHOLD,
            RELEVANCY_THRESHOLD,
            _JUDGE_MODEL_NAME,
            _context_to_retrieval,
            _get_judge,
            _load_context,
            _load_story_input,
            _load_suite,
            _suite_to_output,
        )
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on local env
        raise RuntimeError(_dependency_help(exc)) from exc

    suffix = "_enriched" if mode == "enriched" else "_baseline"
    suite = _load_suite(issue_key, suffix)
    if suite is None:
        raise RuntimeError(f"No generated suite found for {issue_key} ({mode}).")

    story_input = _load_story_input(issue_key)
    actual_output = _suite_to_output(suite)
    judge = _get_judge()
    metrics: list[dict[str, Any]] = []
    context_relevance_threshold = 0.5

    answer_case = LLMTestCase(
        input=story_input,
        actual_output=actual_output,
    )
    answer_metric = AnswerRelevancyMetric(
        threshold=RELEVANCY_THRESHOLD,
        model=judge,
        include_reason=True,
        verbose_mode=False,
    )
    answer_metric.measure(answer_case)
    metrics.append(
        {
            "name": "Answer Relevancy",
            "passed": bool(answer_metric.success),
            "score": float(answer_metric.score or 0.0),
            "threshold": RELEVANCY_THRESHOLD,
            "reason": answer_metric.reason or "(no reason provided)",
        }
    )

    retrieval_context: list[str] = []
    if mode == "enriched":
        context_pkg = _load_context(issue_key)
        if context_pkg is not None:
            retrieval_context = _context_to_retrieval(context_pkg)
            context_case = LLMTestCase(
                input=story_input,
                actual_output=actual_output,
                retrieval_context=retrieval_context,
            )
            context_metric = ContextualRelevancyMetric(
                threshold=context_relevance_threshold,
                model=judge,
                include_reason=True,
                verbose_mode=False,
            )
            context_metric.measure(context_case)
            metrics.append(
                {
                    "name": "Context Relevance",
                    "passed": bool(context_metric.success),
                    "score": float(context_metric.score or 0.0),
                    "threshold": context_relevance_threshold,
                    "reason": context_metric.reason or "(no reason provided)",
                }
            )

            faith_case = LLMTestCase(
                input=story_input,
                actual_output=actual_output,
                retrieval_context=retrieval_context,
            )
            faith_metric = FaithfulnessMetric(
                threshold=FAITHFULNESS_THRESHOLD,
                model=judge,
                include_reason=True,
                verbose_mode=False,
            )
            faith_metric.measure(faith_case)
            metrics.append(
                {
                    "name": "Faithfulness",
                    "passed": bool(faith_metric.success),
                    "score": float(faith_metric.score or 0.0),
                    "threshold": FAITHFULNESS_THRESHOLD,
                    "reason": faith_metric.reason or "(no reason provided)",
                }
            )
        else:
            metrics.append(
                {
                    "name": "Context Relevance",
                    "passed": False,
                    "score": 0.0,
                    "threshold": context_relevance_threshold,
                    "reason": f"No context file found for {issue_key}, so context relevance could not be evaluated.",
                    "skipped": True,
                }
            )
            metrics.append(
                {
                    "name": "Faithfulness",
                    "passed": False,
                    "score": 0.0,
                    "threshold": FAITHFULNESS_THRESHOLD,
                    "reason": f"No context file found for {issue_key}, so faithfulness could not be evaluated.",
                    "skipped": True,
                }
            )
    else:
        metrics.append(
            {
                "name": "Context Relevance",
                "passed": False,
                "score": 0.0,
                "threshold": context_relevance_threshold,
                "reason": "Context relevance is only available for suites evaluated with retrieval context.",
                "skipped": True,
            }
        )

    return {
        "passed": all(metric["passed"] for metric in metrics if not metric.get("skipped")),
        "judge_model": _JUDGE_MODEL_NAME,
        "metrics": metrics,
        "story_input": story_input,
        "actual_output": actual_output,
        "retrieval_context": retrieval_context,
    }


def _build_issue_index() -> list[dict[str, Any]]:
    keys = set()
    keys.update(_keys_from_files(RAW_STORY_DIR, "*.json"))
    keys.update(_keys_from_files(NORMALIZED_DIR, "*.json"))
    keys.update(_keys_from_files(CONTEXT_DIR, "*.json"))
    keys.update(_keys_from_files(GENERATED_DIR, "*_baseline.json", suffix="_baseline"))
    keys.update(_keys_from_files(GENERATED_DIR, "*_enriched.json", suffix="_enriched"))

    indexed = []
    for key in sorted(keys):
        latest_path = _latest_issue_artifact_path(key)
        indexed.append(
            {
                "key": key,
                "raw": (RAW_STORY_DIR / f"{key}.json").exists(),
                "normalized": (NORMALIZED_DIR / f"{key}.json").exists(),
                "context": (CONTEXT_DIR / f"{key}.json").exists(),
                "baseline": (GENERATED_DIR / f"{key}_baseline.json").exists(),
                "enriched": (GENERATED_DIR / f"{key}_enriched.json").exists(),
                "last_updated": _format_mtime(latest_path) if latest_path else None,
                "_sort_ts": latest_path.stat().st_mtime if latest_path else 0,
            }
        )
    indexed.sort(key=lambda item: item["_sort_ts"], reverse=True)
    for item in indexed:
        item.pop("_sort_ts", None)
    return indexed


def _keys_from_files(directory: Path, pattern: str, suffix: str = "") -> set[str]:
    if not directory.exists():
        return set()
    keys = set()
    for path in directory.glob(pattern):
        name = path.stem
        if suffix and name.endswith(suffix):
            name = name[: -len(suffix)]
        keys.add(name.upper())
    return keys


def _latest_issue_artifact_path(issue_key: str) -> Path | None:
    candidates = [
        RAW_STORY_DIR / f"{issue_key}.json",
        NORMALIZED_DIR / f"{issue_key}.json",
        CONTEXT_DIR / f"{issue_key}.json",
        GENERATED_DIR / f"{issue_key}_baseline.json",
        GENERATED_DIR / f"{issue_key}_enriched.json",
    ]
    existing = [path for path in candidates if path.exists()]
    if not existing:
        return None
    return max(existing, key=lambda path: path.stat().st_mtime)


def _format_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")


def _summarize_suite(data: Any, path: Path) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None

    tests = data.get("tests", [])
    if not isinstance(tests, list):
        tests = []

    type_counts = Counter(str(test.get("test_type", "Unknown")) for test in tests)
    priority_counts = Counter(str(test.get("priority", "Unknown")) for test in tests)

    return {
        "path": str(path),
        "story_key": data.get("story_key"),
        "notes": data.get("notes"),
        "test_count": len(tests),
        "type_counts": dict(type_counts),
        "priority_counts": dict(priority_counts),
        "tests": tests,
    }


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def _extract_raw_summary(raw_story: Any) -> str | None:
    if not isinstance(raw_story, dict):
        return None
    return raw_story.get("fields", {}).get("summary")


def _normalize_issue_key(issue_key: str) -> str:
    return issue_key.strip().upper()


def _normalize_mode(mode: str) -> str:
    candidate = mode.strip().lower()
    return candidate if candidate in {"baseline", "enriched"} else "baseline"


def _dependency_help(exc: ModuleNotFoundError) -> str:
    missing = exc.name or "a project dependency"
    return (
        f"Missing dependency: {missing}. Install the project's Python requirements "
        f"and make sure .env is configured before running this action."
    )


# ── Push history helpers ──────────────────────────────────────────────────────

def _load_push_history() -> list[dict[str, Any]]:
    """Return the full push history list, newest-first. Never raises."""
    if not PUSH_HISTORY_FILE.exists():
        return []
    try:
        return json.loads(PUSH_HISTORY_FILE.read_text())
    except Exception:
        return []


def _save_push_history(history: list[dict[str, Any]]) -> None:
    PUSH_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    PUSH_HISTORY_FILE.write_text(json.dumps(history, indent=2))


def get_push_history(issue_key: str | None = None) -> dict[str, Any]:
    """Return push history, optionally filtered to one issue."""
    history = _load_push_history()
    if issue_key:
        key = _normalize_issue_key(issue_key)
        history = [h for h in history if h.get("issue_key") == key]
    return {"history": history, "total_count": len(history)}


def push_selected_tests(issue_key: str, mode: str, indices: list[int]) -> ActionResult:
    """Push a subset of generated test cases to Jira as TestCase subtasks.

    Args:
        issue_key: The parent story key (e.g. AIP-1).
        mode:      "baseline" or "enriched" — which suite to read from.
        indices:   0-based indices of the tests in the suite to push.
                   Empty list → nothing pushed (returns ok=False).

    Returns:
        ActionResult with payload containing:
          created  – list of {title, key, url} for successfully created subtasks.
          failed   – list of {title, error} for failures.
          history  – updated push history for this issue.
    """
    key = _normalize_issue_key(issue_key)
    ui_mode = _normalize_mode(mode)
    add_app_log(f"Push requested for {key} ({ui_mode}): indices={indices}")

    if not indices:
        return ActionResult(ok=False, message="No test cases selected. Tick at least one checkbox.")

    suite_path = GENERATED_DIR / f"{key}_{ui_mode}.json"
    if not suite_path.exists():
        return ActionResult(
            ok=False,
            message=f"No {ui_mode} suite found for {key}. Generate it first.",
        )

    try:
        from src.models.schemas import GeneratedTestSuite
        suite = GeneratedTestSuite.model_validate_json(suite_path.read_text())
    except Exception as exc:
        return _action_error(f"Could not load suite for {key}.", exc)

    # Filter to selected indices, guard against out-of-range
    selected = [suite.tests[i] for i in indices if 0 <= i < len(suite.tests)]
    if not selected:
        return ActionResult(ok=False, message="Selected indices are out of range.")

    try:
        created, failed = asyncio.run(_push_tests_async(selected, key))
    except Exception as exc:
        return _action_error(f"Push failed for {key}.", exc)

    # Record each created test in push history
    timestamp = datetime.now().isoformat(timespec="seconds")
    history = _load_push_history()
    for title, jira_key, url in created:
        history.insert(0, {
            "issue_key": key,
            "mode": ui_mode,
            "timestamp": timestamp,
            "test_title": title,
            "jira_key": jira_key,
            "url": url,
        })
    _save_push_history(history)

    if created:
        try:
            asyncio.run(_fetch_story_async(key))
            add_app_log(f"Refreshed story links for {key} after push.", level="info")
        except Exception as exc:  # pragma: no cover - depends on Jira availability
            add_app_log(
                f"Push succeeded for {key}, but story refresh failed: {exc}",
                level="warning",
            )

    n_ok = len(created)
    n_fail = len(failed)
    msg = f"Pushed {n_ok} test(s) to {key}."
    if n_fail:
        msg += f" {n_fail} failed — see payload for details."
    level = "success" if n_ok > 0 else "warning"
    add_app_log(msg, level=level)
    story_url = ""
    if n_ok > 0:
        try:
            from src.jira.client import JiraClient

            story_url = f"{JiraClient().base_url}/browse/{key}"
        except Exception:
            story_url = ""

    return ActionResult(
        ok=n_ok > 0,
        message=msg,
        payload={
            "created": [{"title": t, "jira_key": k, "url": u} for t, k, u in created],
            "failed": [{"title": t, "error": e} for t, e in failed],
            "story_url": story_url,
            "history": get_push_history()["history"],
            "workspace": discover_workspace(),
            "issue": get_issue_bundle(key),
            "logs": get_app_logs(),
        },
    )


async def _push_tests_async(
    tests: list,
    story_key: str,
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]]]:
    """Create Jira TestCase subtasks for a list of GeneratedTestCase objects.

    Returns:
        (created, failed) where:
          created – list of (title, jira_key, url)
          failed  – list of (title, error_message)
    """
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from push_tests import build_payload  # reuse ADF builder from scripts/
    except ImportError:
        # Fallback inline if scripts/ isn't importable
        from scripts.push_tests import build_payload  # type: ignore[no-reuse-attr]

    from src.jira.client import JiraClient
    import httpx

    project_key = story_key.split("-")[0]
    client = JiraClient()
    created: list[tuple[str, str, str]] = []
    failed: list[tuple[str, str]] = []

    for tc in tests:
        payload = build_payload(tc, project_key)
        try:
            result = await client.create_issue(payload)
            jira_key = result["key"]
            url = f"{client.base_url}/browse/{jira_key}"
            created.append((tc.title, jira_key, url))
            add_app_log(f"Created {jira_key}: {tc.title[:60]}", level="success")
        except httpx.HTTPStatusError as exc:
            err = f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            failed.append((tc.title, err))
            add_app_log(f"Failed to create subtask for '{tc.title[:50]}': {err}", level="error")
        except Exception as exc:
            failed.append((tc.title, str(exc)))
            add_app_log(f"Failed to create subtask for '{tc.title[:50]}': {exc}", level="error")

    return created, failed


def _action_error(prefix: str, exc: Exception) -> ActionResult:
    detail = str(exc).strip() or exc.__class__.__name__
    debug = traceback.format_exc(limit=5)
    add_app_log(f"{prefix} {detail}", level="error")
    return ActionResult(
        ok=False,
        message=f"{prefix} {detail}",
        payload={"debug": debug},
    )
