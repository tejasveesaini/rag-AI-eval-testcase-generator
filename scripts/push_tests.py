"""Push generated test cases to Jira as TestCase subtasks.

Usage:
    python scripts/push_tests.py <issue_key>

For each test case in data/generated/<issue_key>.json this script will:
  1. Build an Atlassian Document Format (ADF) description containing
     preconditions, steps, and expected result.
  2. POST it to Jira as a TestCase subtask parented under <issue_key>.
  3. Print the resulting Jira URL for each created subtask.

The Jira project and issue-type ID are derived from the parent issue key
at runtime so this script works for any project in the same Jira instance.
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx

# ── project root on sys.path ─────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.jira.client import JiraClient                    # noqa: E402
from src.models.schemas import GeneratedTestCase, GeneratedTestSuite  # noqa: E402

# The native "TestCase" subtask issue-type that already exists in the workspace.
_TESTCASE_ISSUETYPE_ID = "10012"


# ── ADF helpers ──────────────────────────────────────────────────────────────

def _adf_text(text: str) -> dict:
    return {"type": "text", "text": text}


def _adf_paragraph(*texts: str) -> dict:
    return {
        "type": "paragraph",
        "content": [_adf_text(t) for t in texts],
    }


def _adf_heading(level: int, text: str) -> dict:
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [_adf_text(text)],
    }


def _adf_list_item(text: str) -> dict:
    return {
        "type": "listItem",
        "content": [_adf_paragraph(text)],
    }


def _adf_ordered_list(items: list[str]) -> dict:
    return {
        "type": "orderedList",
        "content": [_adf_list_item(i) for i in items],
    }


def _adf_bullet_list(items: list[str]) -> dict:
    return {
        "type": "bulletList",
        "content": [_adf_list_item(i) for i in items],
    }


def build_description_adf(tc: GeneratedTestCase) -> dict:
    """Render a GeneratedTestCase as an ADF document for the Jira description field."""
    content: list[dict] = []

    # Preconditions
    if tc.preconditions:
        content.append(_adf_heading(3, "Preconditions"))
        content.append(_adf_bullet_list(tc.preconditions))

    # Steps
    content.append(_adf_heading(3, "Steps"))
    content.append(_adf_ordered_list(tc.steps))

    # Expected result
    content.append(_adf_heading(3, "Expected Result"))
    content.append(_adf_paragraph(tc.expected_result))

    # Metadata line
    content.append(_adf_heading(3, "Metadata"))
    content.append(_adf_paragraph(
        f"Priority: {tc.priority.value}  |  "
        f"Type: {tc.test_type.value}  |  "
        f"Coverage: {tc.coverage_tag}  |  "
        f"Source Story: {tc.source_story}"
    ))

    return {"version": 1, "type": "doc", "content": content}


# ── Jira payload builder ─────────────────────────────────────────────────────

def build_payload(tc: GeneratedTestCase, project_key: str) -> dict:
    return {
        "fields": {
            "project": {"key": project_key},
            "parent": {"key": tc.source_story},
            "issuetype": {"id": _TESTCASE_ISSUETYPE_ID},
            "summary": tc.title,
            "description": build_description_adf(tc),
            "labels": [tc.coverage_tag.replace(" ", "_")],
        }
    }


# ── Main push logic ──────────────────────────────────────────────────────────

async def push_suite(suite: GeneratedTestSuite) -> None:
    project_key = suite.story_key.split("-")[0]
    client = JiraClient()

    print(f"\nPushing {len(suite.tests)} TestCase subtask(s) to {suite.story_key} …\n")

    created: list[tuple[str, str]] = []  # (tc_title, jira_url)
    failed: list[tuple[str, str]] = []   # (tc_title, error)

    for i, tc in enumerate(suite.tests, 1):
        payload = build_payload(tc, project_key)
        print(f"  [{i}/{len(suite.tests)}] Creating: {tc.title[:70]}")
        try:
            result = await client.create_issue(payload)
            key = result["key"]
            base = client.base_url
            url = f"{base}/browse/{key}"
            created.append((tc.title, url))
            print(f"           ✓  {key}  →  {url}")
        except httpx.HTTPStatusError as exc:
            msg = f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            failed.append((tc.title, msg))
            print(f"           ✗  {msg}")
        except Exception as exc:  # noqa: BLE001
            failed.append((tc.title, str(exc)))
            print(f"           ✗  {exc}")

    # Summary
    print(f"\n{'─'*60}")
    print(f"  Created : {len(created)}/{len(suite.tests)}")
    if failed:
        print(f"  Failed  : {len(failed)}")
        for title, err in failed:
            print(f"    • {title[:60]}: {err}")
    print(f"{'─'*60}\n")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/push_tests.py <issue_key>")
        print("       e.g.  python scripts/push_tests.py AIP-2")
        sys.exit(1)

    issue_key = sys.argv[1].upper()
    generated_path = ROOT / "data" / "generated" / f"{issue_key}.json"

    if not generated_path.exists():
        print(f"ERROR: No generated file found at {generated_path}")
        print(f"       Run 'python scripts/generate_tests.py {issue_key}' first.")
        sys.exit(1)

    raw = json.loads(generated_path.read_text())
    suite = GeneratedTestSuite.model_validate(raw)

    asyncio.run(push_suite(suite))


if __name__ == "__main__":
    main()
