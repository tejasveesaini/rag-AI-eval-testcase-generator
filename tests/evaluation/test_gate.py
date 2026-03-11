import pytest
from src.evaluation.gate import passes_gate
from src.models.schemas import GeneratedTestCase, GeneratedTestSuite, Priority, CaseType


def _make_tc(**overrides) -> GeneratedTestCase:
    defaults = dict(
        title="User can log in",
        preconditions=["User is on the login page"],
        steps=["Enter credentials", "Click Login"],
        expected_result="User is redirected to the dashboard",
        priority=Priority.HIGH,
        test_type=CaseType.FUNCTIONAL,
        coverage_tag="AC-1",
        source_story="PROJ-1",
    )
    return GeneratedTestCase(**{**defaults, **overrides})


def _make_suite(tests=None) -> GeneratedTestSuite:
    return GeneratedTestSuite(
        story_key="PROJ-1",
        tests=tests if tests is not None else [_make_tc()],
    )


def test_passes_gate_valid() -> None:
    assert passes_gate(_make_suite()) is True


def test_fails_gate_empty_suite() -> None:
    # pydantic min_length=1 on tests means we test the gate logic
    # by bypassing construction — use model_construct to skip validation
    suite = GeneratedTestSuite.model_construct(story_key="PROJ-1", tests=[])
    assert passes_gate(suite) is False


def test_fails_gate_no_title() -> None:
    assert passes_gate(_make_suite([_make_tc(title="")])) is False


def test_fails_gate_no_steps() -> None:
    tc = GeneratedTestCase.model_construct(
        title="Title",
        steps=[],
        expected_result="Expected",
        source_story="PROJ-1",
        preconditions=[],
        priority=Priority.MEDIUM,
        test_type=CaseType.FUNCTIONAL,
        coverage_tag="",
    )
    assert passes_gate(_make_suite([tc])) is False


def test_fails_gate_no_expected_result() -> None:
    assert passes_gate(_make_suite([_make_tc(expected_result="")])) is False


def test_fails_gate_no_source_story() -> None:
    assert passes_gate(_make_suite([_make_tc(source_story="")])) is False
