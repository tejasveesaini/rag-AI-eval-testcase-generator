"""Discovery pipeline: start from a story, find related issues, index them, query.

This module closes the loop between Jira search and the vector store:

  Story
    │
    ├─▶ 1. Build JQL from summary + AC keywords            [collector]
    │       • keyword search (summary ~ kw1 AND summary ~ kw2 …)
    │       • broad fallback if < _KEYWORD_MIN hits
    │       → raw Jira issue dicts
    │
    ├─▶ 2. Normalize raw dicts → ContextItems              [normalizer]
    │       • strip Jira noise, map to ContextItemType
    │       → list[ContextItem]
    │
    ├─▶ 3. Convert ContextItems → RetrievalDocuments       [retrieval_doc]
    │       → list[RetrievalDocument]
    │
    ├─▶ 4. Embed + upsert into ChromaDB                    [store]
    │       • idempotent upsert — running twice is always safe
    │
    └─▶ 5. Similarity search against current story         [store + query_builder]
            • query = build_query(story) → embed → ANN search
            → list[QueryResult]  (top-N most relevant documents)

Public API
----------
  discover_and_index(story, n_results, client)
      Runs steps 2–5 online (requires Jira credentials + Gemini API key).
      Returns DiscoveryResult.

  discover_and_index_from_key(story_key, n_results, client)
      Convenience wrapper: loads StoryContext from data/normalized/ first,
      then calls discover_and_index().

  DiscoveryResult  — dataclass wrapping retrieved QueryResults + counts.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from src.context.collector import search_related_issues
from src.context.normalizer import normalize_discovered_issues
from src.context.retrieval_doc import from_context_item
from src.models.schemas import ContextItem, RetrievalDocument, StoryContext
from src.retrieval.query_builder import build_query
from src.retrieval.store import QueryResult, query_documents, upsert_documents

logger = logging.getLogger(__name__)

# Default number of results returned by similarity search
_DEFAULT_N = 5

# Minimum Gemini similarity score to include in results (0–1; 1 = identical)
_MIN_SCORE = 0.30


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class DiscoveryResult:
    """Output of the full discovery pipeline for one story.

    Attributes:
        story_key:         The Jira key of the story that was processed.
        discovered_count:  How many new issues were found via JQL search.
        indexed_count:     How many RetrievalDocuments were upserted into Chroma.
        query_results:     Top-N most similar documents from the vector store,
                           sorted by score descending.
        context_items:     ContextItems built from the discovered issues —
                           ready to pass directly into the prompt builder.
    """
    story_key:        str
    discovered_count: int
    indexed_count:    int
    query_results:    list[QueryResult] = field(default_factory=list)
    context_items:    list[ContextItem] = field(default_factory=list)

    def top_results(self, min_score: float = _MIN_SCORE) -> list[QueryResult]:
        """Return only results above min_score threshold."""
        return [r for r in self.query_results if r.score >= min_score]

    def log_summary(self) -> None:
        logger.info(
            "Discovery[%s]: found=%d indexed=%d retrieved=%d (score≥%.2f: %d)",
            self.story_key,
            self.discovered_count,
            self.indexed_count,
            len(self.query_results),
            _MIN_SCORE,
            len(self.top_results()),
        )


# ── Core pipeline ─────────────────────────────────────────────────────────────

async def discover_and_index(
    story: StoryContext,
    n_results: int = _DEFAULT_N,
    client=None,                    # chromadb.ClientAPI — injectable for tests
    already_known_keys: set[str] | None = None,
) -> DiscoveryResult:
    """Run the full discovery pipeline for one story.

    Steps:
      1. Search Jira for related issues via keyword + fallback JQL.
      2. Normalize raw dicts → ContextItems.
      3. Convert ContextItems → RetrievalDocuments.
      4. Upsert into ChromaDB (idempotent).
      5. Run similarity search and return top-N results.

    Args:
        story:             Normalized StoryContext (from data/normalized/).
        n_results:         How many similar documents to retrieve (default 5).
        client:            Optional ChromaDB client for testing.
        already_known_keys: Issue keys already collected via other paths;
                            excluded from the discovery search.

    Returns:
        DiscoveryResult with retrieved documents and context items.
    """
    logger.info("Starting discovery for %s …", story.issue_key)

    # ── Step 1: Search Jira for related issues ────────────────────────────────
    raw_issues = await search_related_issues(
        story_key=story.issue_key,
        summary=story.summary,
        acceptance_criteria=story.acceptance_criteria,
        already_known_keys=already_known_keys,
    )
    logger.info("  [1/4] Jira search returned %d issues", len(raw_issues))

    # ── Step 2: Normalize ─────────────────────────────────────────────────────
    context_items = normalize_discovered_issues(
        raw_issues,
        relevance_hint="keyword/fallback discovery",
    )
    logger.info("  [2/4] Normalized to %d ContextItems", len(context_items))

    # ── Step 3: Convert to RetrievalDocuments ─────────────────────────────────
    docs: list[RetrievalDocument] = []
    seen_doc_ids: set[str] = set()
    for seq, item in enumerate(context_items):
        doc = from_context_item(item, seq=seq)
        if doc.doc_id not in seen_doc_ids:
            docs.append(doc)
            seen_doc_ids.add(doc.doc_id)
    logger.info("  [3/4] Built %d RetrievalDocuments", len(docs))

    # ── Step 4: Upsert into Chroma ────────────────────────────────────────────
    indexed_count = 0
    if docs:
        try:
            indexed_count = upsert_documents(docs, client=client)
            logger.info("  [4/4] Upserted %d documents into ChromaDB", indexed_count)
        except Exception as exc:
            logger.warning("  [4/4] ChromaDB upsert failed: %s — proceeding without index", exc)
    else:
        logger.info("  [4/4] No new documents to upsert")

    # ── Step 5: Similarity search ─────────────────────────────────────────────
    query_text = build_query(story)
    try:
        query_results = query_documents(query_text, n_results=n_results, client=client)
    except Exception as exc:
        logger.warning("Similarity search failed: %s", exc)
        query_results = []

    result = DiscoveryResult(
        story_key=story.issue_key,
        discovered_count=len(raw_issues),
        indexed_count=indexed_count,
        query_results=query_results,
        context_items=context_items,
    )
    result.log_summary()
    return result


def discover_and_index_sync(
    story: StoryContext,
    n_results: int = _DEFAULT_N,
    client=None,
    already_known_keys: set[str] | None = None,
) -> DiscoveryResult:
    """Synchronous wrapper around discover_and_index for non-async callers.

    Uses asyncio.run() — do NOT call from inside a running event loop.
    For async contexts, call discover_and_index() directly.
    """
    return asyncio.run(
        discover_and_index(story, n_results=n_results, client=client,
                           already_known_keys=already_known_keys)
    )


# ── Convenience: load from disk then discover ─────────────────────────────────

def discover_and_index_from_key(
    story_key: str,
    n_results: int = _DEFAULT_N,
    client=None,
) -> DiscoveryResult:
    """Load a normalized story from disk and run the discovery pipeline.

    Expects data/normalized/<story_key>.json to exist.
    Raises FileNotFoundError if the normalized file is missing.
    """
    normalized_path = (
        Path(__file__).resolve().parents[2] / "data" / "normalized" / f"{story_key}.json"
    )
    if not normalized_path.exists():
        raise FileNotFoundError(
            f"Normalized story not found: {normalized_path}\n"
            f"Run: python scripts/fetch_issue.py {story_key}  and  "
            f"python scripts/build_retrieval_index.py {story_key}  first."
        )

    story = StoryContext.model_validate_json(normalized_path.read_text())
    return discover_and_index_sync(story, n_results=n_results, client=client)
