"""CLI: Collect, normalize, and package historical context for a Jira story.

Usage:
    python scripts/collect_context.py <issue_key>

    e.g.  python scripts/collect_context.py AIP-2

Output:
    data/context/<issue_key>.json  — the ContextPackage ready for prompt injection

Pipeline:
    1. collector  → fetch raw related issues from Jira (linked + narrow JQL)
    2. normalizer → convert raw dicts to ContextItems (discard all Jira noise)
    3. packager   → assemble ContextPackage and derive coverage hints
    4. save       → write to data/context/<key>.json
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.context.collector import collect_raw_context     # noqa: E402
from src.context.normalizer import normalize_raw_context  # noqa: E402
from src.context.packager import build_context_package, save_context_package  # noqa: E402
from src.jira.client import JiraClient                    # noqa: E402
from src.jira.ingestor import parse_issue                 # noqa: E402


async def run(issue_key: str) -> None:
    print(f"\nCollecting context for {issue_key} …\n")

    # Step 1: Fetch the main story (needed for packager to derive hints)
    print("  [1/4] Fetching main story …")
    jira = JiraClient()
    raw_story = await jira.get_issue(issue_key)
    story = parse_issue(raw_story)
    print(f"        ✓ story: {story.summary[:70]}")

    # Step 2: Fetch raw related issues
    print("  [2/4] Collecting related issues (linked + narrow JQL) …")
    raw_context = await collect_raw_context(issue_key)
    n_linked = len(raw_context["linked_raw"])
    n_jql    = len(raw_context["jql_raw"])
    print(f"        ✓ linked={n_linked}  jql={n_jql}")

    # Step 3: Normalize
    print("  [3/4] Normalizing related issues …")
    normalized = normalize_raw_context(raw_context)
    linked_items = normalized["linked"]
    jql_items    = normalized["jql"]
    print(f"        ✓ linked_items={len(linked_items)}  jql_items={len(jql_items)}")
    for item in linked_items + jql_items:
        hint = f"  [{item.relevance_hint}]" if item.relevance_hint else ""
        print(f"          • {item.key} [{item.issue_type}] {item.summary[:60]}{hint}")

    # Step 4: Package and save
    print("  [4/4] Building context package …")
    package = build_context_package(story, linked_items, jql_items)
    saved_path = save_context_package(package)
    print(f"        ✓ saved → {saved_path.relative_to(ROOT)}")

    # Summary
    print(f"\n{'─'*60}")
    print(f"  story_key       : {package.story_key}")
    print(f"  linked_defects  : {len(package.linked_defects)}")
    print(f"  historical_tests: {len(package.historical_tests)}")
    print(f"  related_stories : {len(package.related_stories)}")
    print(f"  coverage_hints  : {len(package.coverage_hints)}")
    if package.coverage_hints:
        for h in package.coverage_hints:
            print(f"    → {h}")
    print(f"{'─'*60}\n")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/collect_context.py <issue_key>")
        print("       e.g.  python scripts/collect_context.py AIP-2")
        sys.exit(1)

    issue_key = sys.argv[1].upper()
    asyncio.run(run(issue_key))


if __name__ == "__main__":
    main()
