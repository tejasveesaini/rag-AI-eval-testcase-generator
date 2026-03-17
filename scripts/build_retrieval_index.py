"""CLI: Build a retrieval index for a Jira story and save it to disk.

Loads three inputs (all offline — no Jira API calls):
  1. data/normalized/<KEY>.json     → StoryContext
  2. data/context/<KEY>.json        → ContextPackage   (optional)
  3. data/generated/<KEY>_*.json    → GeneratedTestSuite  (optional, prefers enriched)

Additionally, when --discover is passed (or when data/context/<KEY>.json is absent),
the script runs the discovery pipeline to find related issues via Jira keyword search,
normalize and index them, and then runs a similarity search to find the top-N most
relevant historical stories/tests/bugs for this story.

Produces:
  data/retrieval/<KEY>.json  — JSON array of RetrievalDocuments  (human-readable)
  data/chroma/               — persistent ChromaDB vector store  (machine-readable)

Each document in the output is:
  - short   (body ≤ 300 chars)
  - typed   (source_type: story | bug | historical_test | qa_note)
  - unique  (doc_id is a stable content-addressable key)
  - inspectable (title always human-readable)
  - embedded (Gemini text-embedding-004, 768 dims, stored in Chroma)

Usage:
    python scripts/build_retrieval_index.py <issue_key> [--discover] [--top N]

    e.g.  python scripts/build_retrieval_index.py AIP-2
          python scripts/build_retrieval_index.py AIP-2 --discover
          python scripts/build_retrieval_index.py AIP-2 --discover --top 5
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.context.retrieval_doc import build_retrieval_index   # noqa: E402
from src.models.schemas import (                               # noqa: E402
    ContextPackage,
    GeneratedTestSuite,
    RetrievalDocument,
    SourceType,
    StoryContext,
)
from src.retrieval.store import upsert_documents               # noqa: E402

NORMALIZED_DIR = ROOT / "data" / "normalized"
CONTEXT_DIR    = ROOT / "data" / "context"
GENERATED_DIR  = ROOT / "data" / "generated"
RETRIEVAL_DIR  = ROOT / "data" / "retrieval"

# Type-to-emoji for the pretty-print summary
_TYPE_ICON: dict[SourceType, str] = {
    SourceType.STORY:           "📖",
    SourceType.BUG:             "🐛",
    SourceType.HISTORICAL_TEST: "🧪",
    SourceType.QA_NOTE:         "📝",
}


def _load_story(issue_key: str) -> StoryContext:
    path = NORMALIZED_DIR / f"{issue_key}.json"
    if not path.exists():
        print(f"ERROR: No normalized story at {path}")
        print(f"       Run: python scripts/fetch_issue.py {issue_key}  first.")
        sys.exit(1)
    return StoryContext.model_validate_json(path.read_text())


def _load_package(issue_key: str) -> ContextPackage | None:
    path = CONTEXT_DIR / f"{issue_key}.json"
    if not path.exists():
        print(f"  ⚠  No context package at {path} — skipping linked items and hints.")
        print(f"     Run: python scripts/collect_context.py {issue_key}  to add them.")
        return None
    return ContextPackage.model_validate_json(path.read_text())


def _load_suite(issue_key: str) -> GeneratedTestSuite | None:
    """Prefer enriched suite; fall back to baseline; skip if neither exists."""
    for suffix in ("enriched", "baseline"):
        path = GENERATED_DIR / f"{issue_key}_{suffix}.json"
        if path.exists():
            print(f"  ✓ Generated suite loaded: {path.name}")
            return GeneratedTestSuite.model_validate_json(path.read_text())
    print(f"  ⚠  No generated suite found — skipping historical_test documents.")
    print(f"     Run: python scripts/generate_tests.py {issue_key}  to add them.")
    return None


def _print_index(docs: list[RetrievalDocument], issue_key: str) -> None:
    """Print a compact inspection-ready table of the index."""
    print(f"\n{'─'*72}")
    print(f"  RETRIEVAL INDEX  {issue_key}  ({len(docs)} document(s))")
    print(f"{'─'*72}")

    type_counts: dict[SourceType, int] = {}
    for doc in docs:
        type_counts[doc.source_type] = type_counts.get(doc.source_type, 0) + 1
        icon  = _TYPE_ICON.get(doc.source_type, "•")
        fa    = f"  [{doc.feature_area}]" if doc.feature_area else ""
        print(f"  {icon}  {doc.doc_id:<38}  {doc.title[:32]:<32}{fa}")
        # Print the body indented for easy inspection
        wrapped = (doc.body[:120] + "…") if len(doc.body) > 120 else doc.body
        print(f"       {wrapped}")

    print(f"{'─'*72}")
    summary_parts = []
    for st in SourceType:
        n = type_counts.get(st, 0)
        if n:
            summary_parts.append(f"{st.value}={n}")
    print(f"  Summary: {', '.join(summary_parts)}")
    print(f"{'─'*72}\n")


def _run_discovery(story: StoryContext, n_top: int) -> None:
    """Run the discovery pipeline: keyword JQL → normalize → index → query.

    Discovers related issues from Jira even when they share no metadata tags
    with the current story, indexes them into ChromaDB, then prints the top-N
    most similar documents for inspection.
    """
    from src.retrieval.discovery import discover_and_index_sync  # noqa: E402
    from src.retrieval.store import QueryResult                   # noqa: E402

    print(f"\n{'─'*72}")
    print(f"  DISCOVERY PIPELINE  {story.issue_key}")
    print(f"{'─'*72}")

    result = discover_and_index_sync(story, n_results=n_top)

    print(f"  Jira keyword search : {result.discovered_count} issue(s) found")
    print(f"  ChromaDB upserted   : {result.indexed_count} document(s)")
    print(f"  Similarity results  : {len(result.query_results)} returned\n")

    if not result.query_results:
        print("  (No results — collection may be empty or Gemini API unavailable)")
        print(f"{'─'*72}\n")
        return

    _score_bar = lambda s: "█" * round(s * 20) + "░" * (20 - round(s * 20))

    for i, r in enumerate(result.query_results, 1):
        score_display = f"{r.score:.3f}  [{_score_bar(max(0.0, r.score))}]"
        print(f"  {i}. [{r.source_type.value:<15}]  {r.source_key:<12}  score={score_display}")
        print(f"     Title : {r.title}")
        print(f"     Body  : {r.body[:100]}{'…' if len(r.body) > 100 else ''}")
        print()

    above_threshold = result.top_results()
    print(f"  {len(above_threshold)} result(s) above score ≥ 0.30  (used in prompt context)")
    print(f"{'─'*72}\n")


def main(issue_key: str, run_discovery: bool = False, n_top: int = 5) -> None:
    print(f"\nBuilding retrieval index for {issue_key} …\n")

    story   = _load_story(issue_key)
    package = _load_package(issue_key)
    suite   = _load_suite(issue_key)

    docs = build_retrieval_index(story, package=package, suite=suite)

    # 1. Persist JSON (human-readable, diff-friendly)
    RETRIEVAL_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RETRIEVAL_DIR / f"{issue_key}.json"
    payload  = [doc.model_dump() for doc in docs]
    out_path.write_text(json.dumps(payload, indent=2))

    _print_index(docs, issue_key)
    print(f"  Saved  → {out_path.relative_to(ROOT)}")

    # 2. Upsert into ChromaDB (Gemini embeddings, persistent local store)
    print(f"\n  Upserting {len(docs)} document(s) into ChromaDB …")
    try:
        n = upsert_documents(docs)
        settings_path = Path("data/chroma")
        print(f"  ✓ Upserted {n} document(s) → {settings_path}/")
    except Exception as exc:
        print(f"  ✗ ChromaDB upsert failed: {exc}")
        print("    (JSON index was still saved — re-run after fixing the error.)")
        sys.exit(1)

    # 3. Discovery pipeline (find related issues not captured by context package)
    should_discover = run_discovery or (package is None)
    if should_discover:
        if package is None:
            print(
                "\n  ℹ  No context package found — automatically running discovery "
                "to find related issues via keyword search …"
            )
        try:
            _run_discovery(story, n_top=n_top)
        except Exception as exc:
            print(f"\n  ⚠  Discovery pipeline failed: {exc}")
            print("     (Index was still built — discovery is non-blocking.)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Build a retrieval index for a Jira story.",
    )
    parser.add_argument("issue_key", help="Jira issue key, e.g. AIP-2")
    parser.add_argument(
        "--discover",
        action="store_true",
        default=False,
        help=(
            "Run the keyword-based discovery pipeline after indexing. "
            "Automatically enabled when no context package is present."
        ),
    )
    parser.add_argument(
        "--top",
        type=int,
        default=5,
        dest="n_top",
        help="Number of similarity results to show from discovery (default: 5)",
    )
    args = parser.parse_args()
    main(args.issue_key.upper(), run_discovery=args.discover, n_top=args.n_top)

