"""Tests for GET /stories/{issue_key} — Jira HTTP calls are mocked."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.app import app

client = TestClient(app)

# Re-use the realistic raw fixture already on disk
_RAW_FIXTURE = json.loads(
    (Path(__file__).resolve().parents[2] / "data" / "sample_stories" / "PROJ-1-raw.json").read_text()
)


def _mock_get_issue(payload: dict):
    """Patch JiraClient.get_issue to return payload without any HTTP call."""
    return patch(
        "src.api.routes.JiraClient.get_issue",
        new_callable=AsyncMock,
        return_value=payload,
    )


# ── Happy path ────────────────────────────────────────────────────────────────

def test_get_story_returns_200_and_normalized_shape():
    with _mock_get_issue(_RAW_FIXTURE):
        response = client.get("/stories/PROJ-1")

    assert response.status_code == 200
    body = response.json()
    # All StoryContext fields must be present
    assert body["issue_key"] == "PROJ-1"
    assert body["summary"] == "User can log in with valid credentials"
    assert "log in" in body["description"]
    assert body["acceptance_criteria"] is not None
    assert "labels" in body
    assert "components" in body
    assert "linked_issues" in body


def test_get_story_linked_issues_normalized():
    with _mock_get_issue(_RAW_FIXTURE):
        response = client.get("/stories/PROJ-1")

    links = response.json()["linked_issues"]
    assert len(links) == 1
    assert links[0]["key"] == "PROJ-2"
    assert links[0]["issue_type"] == "Sub-task"


def test_get_story_description_does_not_repeat_ac():
    with _mock_get_issue(_RAW_FIXTURE):
        body = client.get("/stories/PROJ-1").json()

    # AC content must not bleed into description
    if body["acceptance_criteria"] and body["description"]:
        assert body["acceptance_criteria"] not in body["description"]


# ── Error handling ────────────────────────────────────────────────────────────

def test_get_story_404_when_jira_returns_404():
    import httpx
    mock_response = AsyncMock(side_effect=httpx.HTTPStatusError(
        "not found",
        request=httpx.Request("GET", "http://jira/rest/api/3/issue/NOPE-1"),
        response=httpx.Response(404),
    ))
    with patch("src.api.routes.JiraClient.get_issue", mock_response):
        response = client.get("/stories/NOPE-1")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_get_story_422_on_invalid_key_format():
    """Issue key must match [A-Z]+-\\d+ — route-level validation."""
    response = client.get("/stories/not-a-key")
    assert response.status_code == 422
