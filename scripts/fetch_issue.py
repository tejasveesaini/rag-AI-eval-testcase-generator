"""Fetch a single Jira issue, save the raw response, and print the parsed model.

Usage:
    python scripts/fetch_issue.py PROJ-123

The raw JSON is saved to:
    data/sample_stories/<ISSUE_KEY>.json

This file can then be used for offline development and evaluation without
hitting the Jira API again.
"""

import asyncio
import json
import sys
from pathlib import Path

# Allow src/ imports when running as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.jira.client import JiraClient
from src.jira.ingestor import parse_issue


RAW_DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "sample_stories"


async def fetch_and_save(issue_key: str) -> None:
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Fetching {issue_key} from Jira...")
    client = JiraClient()
    raw = await client.get_issue(issue_key)

    # Save the raw response exactly as received — no transformation
    output_path = RAW_DATA_DIR / f"{issue_key}.json"
    output_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False))
    print(f"Raw response saved → {output_path}")

    # Parse into domain model and print it
    story = parse_issue(raw)
    print("\nParsed StoryContext:")
    print(story.model_dump_json(indent=2))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/fetch_issue.py <ISSUE_KEY>")
        sys.exit(1)

    asyncio.run(fetch_and_save(sys.argv[1].upper()))
