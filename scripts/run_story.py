"""End-to-end pipeline for a single Jira story.

Runs all steps in sequence:
  1. Fetch raw issue from Jira                → data/sample_stories/<KEY>.json
  2. Normalize into StoryContext              → data/normalized/<KEY>.json
  3. Collect related context                  → data/context/<KEY>.json
  4. Build + upsert retrieval index           → data/retrieval/<KEY>.json + Chroma
  5. Run discovery (keyword JQL + similarity) → enriches context from ChromaDB
  6. Generate test cases                      → data/generated/<KEY>_enriched.json

Usage:
    python scripts/run_story.py <ISSUE_KEY>

    e.g.  python scripts/run_story.py AIP-1
          python scripts/run_story.py AIP-3
"""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.context.collector import collect_raw_context       # noqa: E402
from src.context.normalizer import normalize_raw_context    # noqa: E402
from src.context.packager import build_context_package, save_context_package  # noqa: E402
from src.context.retrieval_doc import build_retrieval_index  # noqa: E402
from src.generation.generator import generate_test_suite    # noqa: E402
from src.jira.client import JiraClient                      # noqa: E402
from src.jira.ingestor import parse_issue                   # noqa: E402
from src.models.schemas import CaseType, ContextPackage, StoryContext  # noqa: E402
from src.retrieval.discovery import discover_and_index       # noqa: E402
from src.retrieval.store import upsert_documents             # noqa: E402

SAMPLE_DIR    = ROOT / "data" / "sample_stories"
NORMALIZED_DIR = ROOT / "data" / "normalized"
CONTEXT_DIR   = ROOT / "data" / "context"
RETRIEVAL_DIR = ROOT / "data" / "retrieval"
GENERATED_DIR = ROOT / "data" / "generated"


def _sep(label: str = "") -> None:
    width = 64
    if label:
        pad = (width - len(label) - 2) // 2
        print(f"\n{'─' * pad} {label} {'─' * pad}")
    else:
        print("─" * width)


def _print_suite(suite) -> None:
    types_present = {tc.test_type for tc in suite.tests}
    has_negative  = CaseType.NEGATIVE in types_present or CaseType.EDGE_CASE in types_present
    for i, tc in enumerate(suite.tests, 1):
        print(f"  {i}. [{tc.test_type.value:12}] [{tc.priority.value:6}]  {tc.title}")
        if tc.preconditions:
            for pre in tc.preconditions:
                print(f"        PRE  : {pre}")
        for step in tc.steps:
            print(f"        STEP : {step}")
        print(f"        WANT : {tc.expected_result}")
        if tc.coverage_tag:
            print(f"        TAG  : {tc.coverage_tag}")
        print()
    if not has_negative:
        print("  ⚠  No Negative / Edge Case test generated.")
    else:
        print("  ✅ Negative / Edge Case coverage present.")
    if suite.notes:
        print(f"\n  Notes: {suite.notes}")


async def run(issue_key: str) -> None:

    # ── Step 1: Fetch ─────────────────────────────────────────────────────────
    _sep(f"1/6  FETCH  {issue_key}")
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    sample_path = SAMPLE_DIR / f"{issue_key}.json"

    if sample_path.exists():
        print(f"  ✓ Already fetched → {sample_path.relative_to(ROOT)}")
        raw = json.loads(sample_path.read_text())
    else:
        print(f"  Fetching {issue_key} from Jira …")
        jira = JiraClient()
        raw  = await jira.get_issue(issue_key)
        sample_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False))
        print(f"  ✓ Saved → {sample_path.relative_to(ROOT)}")

    # ── Step 2: Normalize ─────────────────────────────────────────────────────
    _sep(f"2/6  NORMALIZE  {issue_key}")
    NORMALIZED_DIR.mkdir(parents=True, exist_ok=True)
    normalized_path = NORMALIZED_DIR / f"{issue_key}.json"

    story: StoryContext = parse_issue(raw)
    normalized_path.write_text(story.model_dump_json(indent=2))
    print(f"  Story   : {story.summary}")
    print(f"  AC      : {(story.acceptance_criteria or '')[:80]}{'…' if len(story.acceptance_criteria or '') > 80 else ''}")
    print(f"  Labels  : {story.labels}")
    print(f"  Linked  : {[li.key for li in story.linked_issues]}")
    print(f"  ✓ Saved → {normalized_path.relative_to(ROOT)}")

    # ── Step 3: Collect context ───────────────────────────────────────────────
    _sep(f"3/6  CONTEXT  {issue_key}")
    raw_context = await collect_raw_context(issue_key)
    normalized  = normalize_raw_context(raw_context)
    linked_items = normalized["linked"]
    jql_items    = normalized["jql"]
    print(f"  linked={len(linked_items)}  jql={len(jql_items)}")
    for item in linked_items + jql_items:
        print(f"    • {item.key} [{item.issue_type}]  {item.summary[:60]}")

    package = build_context_package(story, linked_items, jql_items)
    save_context_package(package)
    print(f"  ✓ Context package saved → data/context/{issue_key}.json")

    # ── Step 4: Build retrieval index ─────────────────────────────────────────
    _sep(f"4/6  INDEX  {issue_key}")
    docs = build_retrieval_index(story, package=package)
    RETRIEVAL_DIR.mkdir(parents=True, exist_ok=True)
    (RETRIEVAL_DIR / f"{issue_key}.json").write_text(
        json.dumps([d.model_dump() for d in docs], indent=2)
    )
    n_upserted = upsert_documents(docs)
    print(f"  ✓ {len(docs)} document(s) built, {n_upserted} upserted into ChromaDB")
    for doc in docs:
        print(f"    [{doc.source_type.value:<15}]  {doc.doc_id}")

    # ── Step 5: Discovery ─────────────────────────────────────────────────────
    _sep(f"5/6  DISCOVER  {issue_key}")
    discovery = await discover_and_index(story, n_results=5)
    print(f"  Jira keyword search : {discovery.discovered_count} issue(s) found")
    print(f"  ChromaDB upserted   : {discovery.indexed_count} new document(s)")
    top = discovery.top_results()
    print(f"  Similarity top-{len(top)} (score ≥ 0.30):")
    for r in top:
        bar = "█" * round(r.score * 20) + "░" * (20 - round(r.score * 20))
        print(f"    [{r.source_type.value:<15}] {r.source_key:<12} {r.score:.3f} [{bar}]")
        print(f"      {r.title}")

    # Merge retrieved items into the package for richer generation
    from src.generation.generator import _query_results_to_context_items, _merge_discovery_into_package  # noqa: E402
    retrieved_items = _query_results_to_context_items(top)
    enriched_package = _merge_discovery_into_package(
        existing=package,
        story_key=issue_key,
        discovered_items=discovery.context_items,
        retrieved_items=retrieved_items,
    )
    print(f"\n  Enriched package: defects={len(enriched_package.linked_defects)}  "
          f"tests={len(enriched_package.historical_tests)}  "
          f"stories={len(enriched_package.related_stories)}")

    # ── Step 6: Generate ──────────────────────────────────────────────────────
    _sep(f"6/6  GENERATE  {issue_key}")
    print("  Calling Gemini …")
    suite = generate_test_suite(story, max_tests=5, context=enriched_package)

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = GENERATED_DIR / f"{issue_key}_enriched.json"
    out_path.write_text(suite.model_dump_json(indent=2))

    _sep(f"RESULTS  {issue_key}  ({len(suite.tests)} tests)")
    _print_suite(suite)
    print(f"\n  ✓ Saved → {out_path.relative_to(ROOT)}")
    _sep()


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/run_story.py <ISSUE_KEY>")
        print("       e.g.  python scripts/run_story.py AIP-1")
        sys.exit(1)
    asyncio.run(run(sys.argv[1].upper()))


if __name__ == "__main__":
    main()
