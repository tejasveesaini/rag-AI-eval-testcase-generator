"""CLI: Query the retrieval index for a story and inspect results.

Builds a focused query from the normalized story (summary + acceptance criteria
+ components + labels) — NOT the raw Jira JSON — and returns the top-N closest
documents from the ChromaDB index.

Purpose
-------
Manual inspection step before using retrieval results in generation:
  - Are the returned items actually relevant?
  - Is there noise or loose connections?
  - Which source_type (old tests / bugs / related stories) is most useful?
  - Do we need tighter metadata filtering?

Usage
-----
    python scripts/query_retrieval.py <issue_key> [--top 3|5] [--type <source_type>]

    e.g.  python scripts/query_retrieval.py AIP-2
          python scripts/query_retrieval.py AIP-2 --top 5
          python scripts/query_retrieval.py AIP-2 --type historical_test
          python scripts/query_retrieval.py AIP-2 --type bug --top 3

Options
-------
  --top N       Return top-N results (default: 5, capped at collection size)
  --type T      Filter by source_type value: story | bug | historical_test | qa_note
  --no-color    Disable ANSI colours (for piping to files)
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.models.schemas import StoryContext                  # noqa: E402
from src.retrieval.query_builder import build_query, build_query_parts  # noqa: E402
from src.retrieval.store import QueryResult, query_documents  # noqa: E402

NORMALIZED_DIR = ROOT / "data" / "normalized"

# ── ANSI helpers ──────────────────────────────────────────────────────────────

_USE_COLOR = True  # toggled by --no-color

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text

BOLD   = lambda t: _c("1",    t)
DIM    = lambda t: _c("2",    t)
GREEN  = lambda t: _c("32",   t)
YELLOW = lambda t: _c("33",   t)
CYAN   = lambda t: _c("36",   t)
RED    = lambda t: _c("31",   t)
MAGENTA= lambda t: _c("35",   t)

# Colour per source_type
_TYPE_COLOR = {
    "story":           CYAN,
    "bug":             RED,
    "historical_test": GREEN,
    "qa_note":         YELLOW,
}
_TYPE_ICON = {
    "story":           "📖",
    "bug":             "🐛",
    "historical_test": "🧪",
    "qa_note":         "📝",
}


# ── Score bar ─────────────────────────────────────────────────────────────────

def _score_bar(score: float, width: int = 20) -> str:
    """Return an ASCII progress bar for a score in [0, 1]."""
    filled = max(0, min(width, round(score * width)))
    bar    = "█" * filled + "░" * (width - filled)
    pct    = f"{score * 100:5.1f}%"
    return f"[{bar}] {pct}"


# ── Relevance hint ────────────────────────────────────────────────────────────

def _relevance_label(score: float) -> str:
    if score >= 0.85:
        return GREEN("● Strong match")
    if score >= 0.70:
        return GREEN("◉ Good match")
    if score >= 0.50:
        return YELLOW("◎ Moderate match")
    return RED("○ Weak / noisy")


# ── Result printer ────────────────────────────────────────────────────────────

def _print_result(rank: int, r: QueryResult, query_source_key: str) -> None:
    stype     = r.source_type.value
    colorizer = _TYPE_COLOR.get(stype, DIM)
    icon      = _TYPE_ICON.get(stype, "•")

    # Header
    print(f"\n  {BOLD(f'#{rank}')}  {icon} {colorizer(stype.upper())}  "
          f"{DIM(r.doc_id)}")

    # Score
    print(f"     Score:  {_score_bar(r.score)}  {_relevance_label(r.score)}")

    # Title
    print(f"     Title:  {r.title}")

    # Body preview — first 120 chars
    body_preview = r.body[:120].replace("\n", " ").strip()
    if len(r.body) > 120:
        body_preview += " …"
    print(f"     Body:   {DIM(body_preview)}")

    # Metadata extras
    extras: list[str] = []
    if r.feature_area:
        extras.append(f"AC={r.feature_area}")
    if r.components:
        extras.append(f"components=[{', '.join(r.components)}]")
    if r.labels:
        extras.append(f"labels=[{', '.join(r.labels)}]")
    if extras:
        print(f"     Meta:   {DIM('  •  '.join(extras))}")

    # Cross-story flag
    if r.source_key != query_source_key:
        print(f"     {YELLOW('⚠  Cross-story result')} "
              f"(source_key={r.source_key!r}, queried={query_source_key!r})")


# ── Summary section ───────────────────────────────────────────────────────────

def _print_summary(results: list[QueryResult], query_source_key: str) -> None:
    print(f"\n{'─' * 60}")
    print(BOLD("  Inspection notes"))
    print(f"{'─' * 60}")

    # Count by source_type
    counts: dict[str, int] = {}
    for r in results:
        counts[r.source_type.value] = counts.get(r.source_type.value, 0) + 1

    print("  Source-type breakdown:")
    for stype, n in sorted(counts.items(), key=lambda x: -x[1]):
        icon = _TYPE_ICON.get(stype, "•")
        bar  = "■" * n
        print(f"    {icon}  {stype:<20} {bar} ({n})")

    # Cross-story contamination
    cross = [r for r in results if r.source_key != query_source_key]
    if cross:
        print(f"\n  {YELLOW('⚠  Cross-story results detected')} "
              f"({len(cross)}/{len(results)}):")
        for r in cross:
            print(f"     • {r.doc_id}  (source_key={r.source_key!r})")
        print("     → consider adding where={\"source_key\": {\"$eq\": ...}} filter")
    else:
        print(f"\n  {GREEN('✓  All results are from the queried story')} ({query_source_key})")

    # Score quality
    weak = [r for r in results if r.score < 0.50]
    if weak:
        print(f"\n  {YELLOW('⚠  Weak results (score < 0.50)')}: {len(weak)}/{len(results)}")
        print("     → consider reducing n_results or adding a source_type filter")

    high = [r for r in results if r.score >= 0.85]
    if high:
        print(f"\n  {GREEN('✓  Strong matches (score ≥ 0.85)')}: {len(high)}/{len(results)}")

    # Suggestion for most useful type
    if counts:
        best_type = max(counts, key=lambda k: counts[k])
        print(f"\n  Most represented type in results: "
              f"{_TYPE_ICON.get(best_type, '')} {BOLD(best_type)}")
        if best_type == "historical_test":
            print("     → historical tests drive strong grounding; "
                  "use as-is in prompt context")
        elif best_type == "bug":
            print("     → bug results → add negative / regression test cases")
        elif best_type == "qa_note":
            print("     → qa_note results → surface known risks in test coverage")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Inspect retrieval results for a story before using in generation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python scripts/query_retrieval.py AIP-2
              python scripts/query_retrieval.py AIP-2 --top 3
              python scripts/query_retrieval.py AIP-2 --type historical_test
              python scripts/query_retrieval.py AIP-2 --type bug --top 3
        """),
    )
    p.add_argument("issue_key", help="Jira issue key (e.g. AIP-2)")
    p.add_argument("--top",  type=int, default=5, metavar="N",
                   help="Number of results to return (default: 5)")
    p.add_argument("--type", dest="source_type", default=None,
                   choices=["story", "bug", "historical_test", "qa_note"],
                   metavar="TYPE", help="Filter by source_type")
    p.add_argument("--no-color", dest="no_color", action="store_true",
                   help="Disable ANSI colour output")
    return p.parse_args()


def main() -> None:
    global _USE_COLOR

    args = _parse_args()
    if args.no_color:
        _USE_COLOR = False

    issue_key = args.issue_key.upper()

    # ── Load normalized story ─────────────────────────────────────────────────
    story_path = NORMALIZED_DIR / f"{issue_key}.json"
    if not story_path.exists():
        print(f"ERROR: {story_path} not found.")
        print(f"       Run: python scripts/fetch_issue.py {issue_key}  first.")
        sys.exit(1)

    story = StoryContext.model_validate_json(story_path.read_text())

    # ── Build query ───────────────────────────────────────────────────────────
    parts = build_query_parts(story)
    query = build_query(story)

    print(f"\n{'═' * 60}")
    print(BOLD(f"  Retrieval inspection: {issue_key}"))
    print(f"{'═' * 60}")

    print(f"\n  {BOLD('Query components')}")
    print(f"    Summary:  {parts['summary']}")
    if parts["acceptance_criteria"]:
        ac_wrapped = textwrap.fill(
            parts["acceptance_criteria"], width=56,
            initial_indent="              ",
            subsequent_indent="              ",
        ).lstrip()
        print(f"    AC:       {ac_wrapped}")
    if parts["components"]:
        print(f"    Components: {parts['components']}")
    if parts["labels"]:
        print(f"    Labels:   {parts['labels']}")

    print(f"\n  {BOLD('Full query string')} ({len(query)} chars):")
    wrapped_query = textwrap.fill(query, width=56,
                                  initial_indent="    ",
                                  subsequent_indent="    ")
    print(DIM(wrapped_query))

    # ── Filters ───────────────────────────────────────────────────────────────
    where: dict | None = None
    if args.source_type:
        where = {"source_type": {"$eq": args.source_type}}

    filter_note = f"  filter={where}" if where else "  no filter"
    print(f"\n  Top {args.top} results  {DIM(filter_note)}")

    # ── Query ─────────────────────────────────────────────────────────────────
    print(f"\n  Querying ChromaDB …")
    try:
        results = query_documents(
            query,
            n_results=args.top,
            where=where,
        )
    except Exception as exc:
        print(f"\n  {RED('ERROR')}: {exc}")
        print("  Make sure you have run: python scripts/build_retrieval_index.py "
              f"{issue_key}")
        sys.exit(1)

    if not results:
        print(f"\n  {YELLOW('No results found.')}")
        if not where:
            print("  The collection may be empty — run build_retrieval_index.py first.")
        else:
            print(f"  Try removing the --type filter (no docs of type {args.source_type!r}).")
        sys.exit(0)

    print(f"{'─' * 60}")

    for rank, r in enumerate(results, start=1):
        _print_result(rank, r, issue_key)

    _print_summary(results, issue_key)

    print(f"\n{'═' * 60}\n")


if __name__ == "__main__":
    main()
