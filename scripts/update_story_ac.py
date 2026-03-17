"""Update AIP-12 through AIP-16 with acceptance criteria in their description.

This Jira instance has no dedicated AC custom field, so AC is embedded in the
description as an ADF document with a prose paragraph followed by a clearly
labelled "Acceptance Criteria" heading and bullet list.

Usage:
    .venv/bin/python scripts/update_story_ac.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx
from src.jira.client import JiraClient


# ── Per-story content ─────────────────────────────────────────────────────────

STORIES: list[dict] = [
    {
        "key": "AIP-12",
        "summary": "User can access their old conversations.",
        "description": (
            "As a user, I want to be able to view my previous conversations "
            "so that I can refer back to past interactions without losing context."
        ),
        "ac": [
            "Given I am logged in, when I navigate to the conversations section, "
            "then I see a chronological list of all my past conversations.",
            "Each conversation entry shows the date, time, and a short preview of the last message.",
            "Clicking a conversation opens the full message history in read-only mode.",
            "Conversations are retained for at least 90 days from the last message.",
            "If I have no previous conversations, an appropriate empty-state message is shown.",
        ],
    },
    {
        "key": "AIP-13",
        "summary": "User can delete their associated data.",
        "description": (
            "As a user, I want to permanently delete all personal data associated "
            "with my account so that I have full control over my privacy."
        ),
        "ac": [
            "Given I am logged in, when I request data deletion, then I am shown a confirmation dialog "
            "that clearly states the action is irreversible.",
            "After confirming, all personal data (profile, conversations, preferences) is deleted within 30 days.",
            "I receive an email confirmation once the deletion is complete.",
            "I cannot log in with the deleted account after the deletion is processed.",
            "The system complies with GDPR Article 17 (right to erasure).",
        ],
    },
    {
        "key": "AIP-14",
        "summary": "The response time on chat should never be more than 10 seconds.",
        "description": (
            "As a user, I expect every chat response to be delivered within 10 seconds "
            "so that the experience remains responsive and usable."
        ),
        "ac": [
            "Given I send a message, the first token of the AI response must appear within 10 seconds "
            "under normal network conditions (< 100 ms RTT).",
            "A visible loading indicator is shown while a response is being generated.",
            "If the 10-second threshold is exceeded, the user sees a clear timeout message with a retry option.",
            "P95 end-to-end response latency is ≤ 10 seconds as measured in production monitoring.",
            "The SLA applies to all chat entry points (web, mobile, embedded widget).",
        ],
    },
    {
        "key": "AIP-15",
        "summary": "User should be able to continue their existing chats.",
        "description": (
            "As a user, I want to resume any of my previous chat sessions "
            "so that I can continue conversations where I left off."
        ),
        "ac": [
            "Given I have one or more previous chat sessions, when I open the chat interface, "
            "I can select any past session to resume it.",
            "Resuming a session loads the full prior message history and allows me to send new messages.",
            "The AI model receives the prior context so its responses are coherent with earlier exchanges.",
            "Sessions are resumable for at least 30 days after the last message.",
            "I can start a brand-new chat at any time without affecting my existing sessions.",
        ],
    },
    {
        "key": "AIP-16",
        "summary": "User should be able to login using email and passcode.",
        "description": (
            "As a user, I want to authenticate with my email address and a passcode "
            "so that I can access my account securely without a full password."
        ),
        "ac": [
            "Given I am on the login screen, I can enter my registered email address and a 6-digit numeric passcode.",
            "If the credentials are correct, I am authenticated and redirected to the home screen.",
            "If the credentials are incorrect, I see a clear error message; the account locks after 5 failed attempts.",
            "A 'Forgot passcode' flow allows me to reset my passcode via a verified email link.",
            "Passcode transmission is encrypted in transit (TLS 1.2+).",
            "The login flow is accessible (WCAG 2.1 AA compliant).",
        ],
    },
]


# ── ADF helpers ───────────────────────────────────────────────────────────────

def _adf_doc(description: str, ac_bullets: list[str]) -> dict:
    """Build an ADF document with a description paragraph + AC heading + bullet list."""
    return {
        "version": 1,
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": description}],
            },
            {
                "type": "heading",
                "attrs": {"level": 3},
                "content": [{"type": "text", "text": "Acceptance Criteria"}],
            },
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": bullet}],
                            }
                        ],
                    }
                    for bullet in ac_bullets
                ],
            },
        ],
    }


# ── Main ──────────────────────────────────────────────────────────────────────

async def update_story(client: JiraClient, story: dict) -> None:
    payload = {
        "fields": {
            "description": _adf_doc(story["description"], story["ac"]),
        }
    }
    headers = {**client._headers, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.put(
                f"{client.base_url}/rest/api/3/issue/{story['key']}",
                headers=headers,
                json=payload,
            )
            if r.status_code == 204:
                print(f"  ✓  {story['key']}  updated — {story['summary']}")
            else:
                print(f"  ✗  {story['key']}  HTTP {r.status_code}: {r.text[:200]}")
    except Exception as exc:
        print(f"  ✗  {story['key']}  error: {exc}")


async def main() -> None:
    client = JiraClient()
    print(f"\nAdding acceptance criteria to {len(STORIES)} stories…\n")
    for story in STORIES:
        await update_story(client, story)
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
