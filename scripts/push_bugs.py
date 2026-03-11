"""Create 2 random Bug issues in Jira, each linked to a TestCase and the feature story."""

import asyncio
import random
import sys
from pathlib import Path
import httpx
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.jira.client import JiraClient

BUG_TYPE_ID = "10003"
PROJECT_KEY = "AIP"
FEATURE_KEY = "AIP-2"
TESTCASE_KEYS = ["AIP-4", "AIP-5"]

BUG_SUMMARIES = [
    "Chat disclaimer not visible on Safari",
    "Warning message disappears after sending message",
    "Security disclaimer text truncated on mobile",
    "Chat input allows forbidden characters",
    "Disclaimer not shown for returning users"
]

BUG_DESCRIPTIONS = [
    [
        ("Steps to Reproduce", ["Open chat widget in Safari.", "Observe disclaimer area."]),
        ("Expected", ["Security disclaimer is visible."]),
        ("Actual", ["Disclaimer area is blank — no text rendered."]),
    ],
    [
        ("Steps to Reproduce", ["Open the chat window.", "Send any message via the input box."]),
        ("Expected", ["Warning message remains visible throughout the session."]),
        ("Actual", ["Warning disappears after the first message is sent."]),
    ],
    [
        ("Steps to Reproduce", ["Open chat widget on a mobile device (320px viewport)."]),
        ("Expected", ["Full disclaimer text is shown without truncation."]),
        ("Actual", ["Text is clipped after the first sentence."]),
    ],
    [
        ("Steps to Reproduce", ["Click inside the chat input.", "Type special characters: <script>alert(1)</script>"]),
        ("Expected", ["Input is sanitised; special characters are rejected or escaped."]),
        ("Actual", ["Characters are accepted and reflected in the chat."]),
    ],
    [
        ("Steps to Reproduce", ["Start a chat session.", "Close and reopen the chat widget."]),
        ("Expected", ["Disclaimer is displayed again on reopen."]),
        ("Actual", ["Disclaimer is not shown for returning users within the same session."]),
    ],
]


def _adf_doc(sections: list[tuple[str, list[str]]]) -> dict:
    """Convert a list of (heading, [bullet…]) tuples into an ADF document."""
    content = []
    for heading, bullets in sections:
        content.append({
            "type": "heading",
            "attrs": {"level": 3},
            "content": [{"type": "text", "text": heading}]
        })
        content.append({
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": b}]}]
                }
                for b in bullets
            ]
        })
    return {"version": 1, "type": "doc", "content": content}


async def push_bug(summary, description_sections, testcase_key):
    client = JiraClient()
    payload = {
        "fields": {
            "project": {"key": PROJECT_KEY},
            "issuetype": {"id": BUG_TYPE_ID},
            "summary": summary,
            "description": _adf_doc(description_sections),
            "labels": ["auto-bug"],
        }
    }
    try:
        result = await client.create_issue(payload)
        key = result["key"]
        url = f"{client.base_url}/browse/{key}"
        print(f"✓ Created Bug: {key} → {url}")
        # Now link to feature and testcase
        await link_issue(client, key, FEATURE_KEY)
        await link_issue(client, key, testcase_key)
    except Exception as exc:
        print(f"✗ Failed: {exc}")

async def link_issue(client, from_key, to_key):
    # POST /rest/api/3/issueLink
    payload = {
        "type": {"name": "Relates"},
        "inwardIssue": {"key": from_key},
        "outwardIssue": {"key": to_key}
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{client.base_url}/rest/api/3/issueLink", headers=client._headers, json=payload)
            if r.status_code == 201:
                print(f"  ✓ Linked {from_key} ↔ {to_key}")
            else:
                print(f"  ✗ Link failed: {r.status_code} {r.text[:100]}")
    except Exception as exc:
        print(f"  ✗ Link error: {exc}")
async def main():
    # Delete the test issue we accidentally created (AIP-9) if it exists
    used_summaries: set[str] = set()
    for testcase_key in TESTCASE_KEYS:
        available = [s for s in BUG_SUMMARIES if s not in used_summaries]
        summary = random.choice(available)
        used_summaries.add(summary)
        # pick matching description by index
        idx = BUG_SUMMARIES.index(summary)
        description_sections = BUG_DESCRIPTIONS[idx]
        await push_bug(summary, description_sections, testcase_key)

if __name__ == "__main__":
    asyncio.run(main())
