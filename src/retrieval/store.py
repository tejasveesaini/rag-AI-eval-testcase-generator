"""ChromaDB vector store — persistent local storage for RetrievalDocuments.

Architecture
------------
One ChromaDB collection called ``COLLECTION_NAME`` holds every document across
all indexed stories.  Chroma handles both embedding storage and ANN search;
we supply vectors from the Gemini embedder so Chroma never calls an external
API on its own.

Document layout in Chroma
--------------------------
  id         → doc_id  (e.g. "AIP-2#story")
  document   → body    (the text Chroma stores alongside the vector)
  embedding  → float vector from embed_documents()
  metadata   → all non-body fields as a flat dict:
                 source_type, source_key, title,
                 components (JSON string), labels (JSON string),
                 feature_area (or "")

Metadata lists (components, labels) are serialised as JSON strings because
Chroma metadata values must be scalar (str | int | float | bool).

Public API
----------
  get_collection(client?)        → chromadb.Collection
  upsert_documents(docs, client?) → int  (number of docs upserted)
  query_documents(text, n, where, client?) → list[QueryResult]

QueryResult is a plain dataclass so callers never import chromadb types.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import chromadb
from chromadb.api import ClientAPI

from src.config import get_settings
from src.models.schemas import RetrievalDocument, SourceType
from src.retrieval.embedder import embed_documents, embed_query

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

COLLECTION_NAME = "retrieval_docs"

# Cosine distance is the right choice for Gemini embeddings (unit-normalised).
_DISTANCE = "cosine"


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class QueryResult:
    """One result returned by query_documents().

    distance  — cosine distance [0, 2]; lower = more similar.
    score     — convenience: 1 - distance, in [−1, 1]; higher = more similar.
    """

    doc_id:      str
    source_type: SourceType
    source_key:  str
    title:       str
    body:        str
    distance:    float
    score:       float
    feature_area: str | None = None
    components:  list[str]   = field(default_factory=list)
    labels:      list[str]   = field(default_factory=list)


# ── ChromaDB client factory ───────────────────────────────────────────────────

def _make_persistent_client() -> ClientAPI:
    """Return a persistent ChromaDB client rooted at settings.chroma_dir."""
    settings = get_settings()
    db_path  = Path(settings.chroma_dir)
    # Resolve relative paths against the project root (two levels up from src/)
    if not db_path.is_absolute():
        db_path = Path(__file__).resolve().parents[2] / db_path
    db_path.mkdir(parents=True, exist_ok=True)
    logger.debug("ChromaDB persistent path: %s", db_path)
    return chromadb.PersistentClient(path=str(db_path))


# ── Metadata serialisation ────────────────────────────────────────────────────

def _to_metadata(doc: RetrievalDocument) -> dict[str, Any]:
    """Flatten a RetrievalDocument into a Chroma-compatible metadata dict.

    Lists are JSON-encoded because Chroma metadata values must be scalar.
    None values become empty string so Chroma filters work predictably.
    """
    return {
        "source_type": doc.source_type.value,
        "source_key":  doc.source_key,
        "title":       doc.title,
        "components":  json.dumps(doc.components),
        "labels":      json.dumps(doc.labels),
        "feature_area": doc.feature_area or "",
    }


def _from_metadata(
    doc_id: str,
    body: str,
    meta: Any,
    distance: float,
) -> QueryResult:
    """Reconstruct a QueryResult from raw Chroma result fields."""
    components = json.loads(meta.get("components", "[]"))
    labels     = json.loads(meta.get("labels",     "[]"))
    fa         = meta.get("feature_area") or None

    return QueryResult(
        doc_id=doc_id,
        source_type=SourceType(meta["source_type"]),
        source_key=meta["source_key"],
        title=meta["title"],
        body=body,
        distance=distance,
        score=round(1.0 - distance, 6),
        feature_area=fa,
        components=components,
        labels=labels,
    )


# ── Public API ────────────────────────────────────────────────────────────────

def get_collection(
    client: ClientAPI | None = None,
    collection_name: str = COLLECTION_NAME,
) -> chromadb.Collection:
    """Return (or create) the shared retrieval_docs collection.

    Uses cosine distance so that Gemini's unit-normalised vectors are
    compared correctly.

    Args:
        client:          Optional ChromaDB client.  When None, a persistent
                         client rooted at settings.chroma_dir is used.  Pass
                         an ephemeral client (``chromadb.EphemeralClient()``)
                         in tests.
        collection_name: Override the collection name.  Used in tests to get
                         fully isolated namespaces (Chroma EphemeralClient
                         shares one in-process backend across instances).
    """
    if client is None:
        client = _make_persistent_client()
    return client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": _DISTANCE},
    )


def upsert_documents(
    docs: list[RetrievalDocument],
    client: ClientAPI | None = None,
    collection_name: str = COLLECTION_NAME,
) -> int:
    """Embed and upsert a list of RetrievalDocuments into Chroma.

    Idempotent: upserting the same doc_id overwrites the existing record.
    This means running ``build_retrieval_index`` twice is always safe.

    Args:
        docs:            RetrievalDocuments to index.  Must be non-empty.
        client:          Optional ChromaDB client (see get_collection).
        collection_name: Override the collection name (used in tests).

    Returns:
        Number of documents upserted.

    Raises:
        ValueError:     if docs is empty.
        EmbeddingError: if the Gemini Embeddings API fails.
    """
    if not docs:
        raise ValueError("upsert_documents requires at least one document.")

    collection = get_collection(client, collection_name)
    bodies     = [doc.body for doc in docs]

    logger.info("Embedding %d document(s) for upsert …", len(docs))
    vectors = embed_documents(bodies)

    collection.upsert(
        ids        =[doc.doc_id   for doc in docs],
        documents  =bodies,
        embeddings =cast(Any, vectors),
        metadatas  =[_to_metadata(doc) for doc in docs],
    )

    logger.info("Upserted %d document(s) into collection %r.", len(docs), collection_name)
    return len(docs)


def query_documents(
    text: str,
    n_results: int = 5,
    where: dict[str, Any] | None = None,
    client: ClientAPI | None = None,
    collection_name: str = COLLECTION_NAME,
) -> list[QueryResult]:
    """Embed a query and return the top-n most similar documents.

    Args:
        text:            Query string — embedded with RETRIEVAL_QUERY task type.
        n_results:       Maximum number of results to return (default 5).
        where:           Optional Chroma metadata filter dict, e.g.
                         ``{"source_type": {"$eq": "bug"}}``.
        client:          Optional ChromaDB client (see get_collection).
        collection_name: Override the collection name (used in tests).

    Returns:
        List of QueryResult sorted by ascending distance (closest first).
        Empty list if the collection has no documents.

    Raises:
        EmbeddingError: if the Gemini Embeddings API fails.
    """
    collection = get_collection(client, collection_name)

    if collection.count() == 0:
        logger.warning("query_documents called on empty collection %r.", collection_name)
        return []

    query_vec = embed_query(text)

    kwargs: dict[str, Any] = {
        "query_embeddings": [query_vec],
        "n_results":        min(n_results, collection.count()),
        "include":          ["documents", "metadatas", "distances"],
    }
    if where:
        kwargs["where"] = where

    raw = collection.query(**kwargs)

    results: list[QueryResult] = []
    ids        = raw["ids"][0]
    documents  = raw["documents"][0]        # type: ignore[index]
    metadatas  = raw["metadatas"][0]        # type: ignore[index]
    distances  = raw["distances"][0]        # type: ignore[index]

    for doc_id, body, meta, dist in zip(ids, documents, metadatas, distances):
        results.append(_from_metadata(doc_id, body, meta, dist))

    return results
