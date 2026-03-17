"""Push new Story-type issues to Jira.

Usage:
    .venv/bin/python scripts/push_stories.py

Creates one Jira issue per entry in STORIES using the project's "New Feature"
issue type (ID 10004).  Prints the created key and browse URL for each story.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.jira.client import JiraClient

PROJECT_KEY = "AIP"
STORY_ISSUETYPE_ID = "10004"  # "New Feature" — the story-level type in this project

# ── Stories to create ──────────────────────────────────────────────────────────
STORIES: list[dict] = [
    {
        "summary": "User can access their old conversations.",
        "description": "As a user, I want to be able to view my previous conversations "
                       "so that I can refer back to past interactions without losing context.",
        "labels": ["history", "conversations"],
    },
    {
        "summary": "User can delete their associated data.",
        "description": "As a user, I want to permanently delete all personal data associated "
                       "with my account so that I have full control over my privacy.",
        "labels": ["privacy", "data-deletion", "gdpr"],
    },
    {
        "summary": "The response time on chat should never be more than 10 seconds.",
        "description": "As a user, I expect every chat response to be delivered within 10 seconds "
                       "so that the experience remains responsive and usable.",
        "labels": ["performance", "chat", "sla"],
    },
    {
        "summary": "User should be able to continue their existing chats.",
        "description": "As a user, I want to resume any of my previous chat sessions "
                       "so that I can continue conversations where I left off.",
        "labels": ["chat", "continuity"],
    },
    {
        "summary": "User should be able to login using email and passcode.",
        "description": "As a user, I want to authenticate with my email address and a passcode "
                       "so that I can access my account securely without a full password.",
        "labels": ["authentication", "login", "security"],
    },
]


def _adf_paragraph(text: str) -> dict:
    """Wrap a plain string in a minimal ADF document."""
    return {
        "version": 1,
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


async def push_story(client: JiraClient, story: dict) -> None:
    payload = {
        "fields": {
            "project": {"key": PROJECT_KEY},
            "issuetype": {"id": STORY_ISSUETYPE_ID},
            "summary": story["summary"],
            "description": _adf_paragraph(story["description"]),
            "labels": story.get("labels", []),
        }
    }
    try:
        result = await client.create_issue(payload)
        key = result["key"]
        url = f"{client.base_url}/browse/{key}"
        print(f"  ✓  {key}  →  {url}")
        print(f"     {story['summary']}")
    except Exception as exc:
        print(f"  ✗  FAILED — {story['summary']}")
        print(f"     {exc}")


async def main() -> None:
    client = JiraClient()
    print(f"\nPushing {len(STORIES)} stories to Jira project [{PROJECT_KEY}]…\n")
    for story in STORIES:
        await push_story(client, story)
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
