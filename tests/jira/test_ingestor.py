"""Tests for src/jira/ingestor.py — uses fixture data, no HTTP calls."""

import json
from pathlib import Path

from src.jira.ingestor import parse_issue, _adf_to_text

FIXTURES = Path(__file__).resolve().parents[2] / "data" / "sample_stories"


def _load(filename: str) -> dict:
    return json.loads((FIXTURES / filename).read_text())


# ── ADF parser ────────────────────────────────────────────────────────────────

def test_adf_to_text_simple_paragraph() -> None:
    adf = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Hello world"}],
            }
        ],
    }
    result = _adf_to_text(adf)
    assert result == "Hello world"


def test_adf_to_text_none_returns_none() -> None:
    assert _adf_to_text(None) is None


# ── parse_issue ───────────────────────────────────────────────────────────────

def test_parse_issue_key() -> None:
    raw = _load("PROJ-1-raw.json")
    story = parse_issue(raw)
    assert story.issue_key == "PROJ-1"


def test_parse_issue_summary() -> None:
    raw = _load("PROJ-1-raw.json")
    story = parse_issue(raw)
    assert story.summary == "User can log in with valid credentials"


def test_parse_issue_description_extracted_from_adf() -> None:
    raw = _load("PROJ-1-raw.json")
    story = parse_issue(raw)
    assert story.description is not None
    assert "log in" in story.description


def test_parse_issue_acceptance_criteria() -> None:
    raw = _load("PROJ-1-raw.json")
    story = parse_issue(raw)
    assert story.acceptance_criteria is not None
    assert "redirected to the dashboard" in story.acceptance_criteria


def test_parse_issue_labels() -> None:
    raw = _load("PROJ-1-raw.json")
    story = parse_issue(raw)
    assert "authentication" in story.labels
    assert "mvp" in story.labels


def test_parse_issue_components() -> None:
    raw = _load("PROJ-1-raw.json")
    story = parse_issue(raw)
    assert "Frontend" in story.components
    assert "Auth Service" in story.components


def test_parse_issue_linked_issues() -> None:
    raw = _load("PROJ-1-raw.json")
    story = parse_issue(raw)
    assert len(story.linked_issues) == 1
    link = story.linked_issues[0]
    assert link.key == "PROJ-2"
    assert link.issue_type == "Sub-task"
    assert "JWT" in link.summary


def test_parse_issue_no_linked_issues() -> None:
    raw = _load("PROJ-1-raw.json")
    raw["fields"]["issuelinks"] = []
    story = parse_issue(raw)
    assert story.linked_issues == []


def test_parse_issue_missing_optional_fields() -> None:
    """Minimal payload — only key + summary — should not raise."""
    raw = {"key": "PROJ-99", "fields": {"summary": "Minimal story"}}
    story = parse_issue(raw)
    assert story.issue_key == "PROJ-99"
    assert story.description is None
    assert story.acceptance_criteria is None
    assert story.labels == []
    assert story.components == []
    assert story.linked_issues == []
