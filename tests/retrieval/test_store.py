"""Tests for src/retrieval/store.py.

Strategy
--------
- ChromaDB is exercised with a shared EphemeralClient.
- Each test gets its own unique collection name (via the `col_name` fixture)
  so tests are fully isolated — EphemeralClient shares one in-process backend
  across calls, so isolation must come from collection naming.
- The Gemini embedder is mocked so tests run offline and deterministically.
- Vectors are low-dimensional (4-d) to keep fixture setup fast.

Coverage:
  get_collection      — creates collection with cosine space; idempotent
  upsert_documents    — stores docs; count increases; idempotent on re-upsert
  query_documents     — returns results; respects n_results; empty collection
  metadata filtering  — where= filter narrows results by source_type / source_key
  metadata round-trip — components, labels, feature_area survive upsert -> col.get
  deduplication       — upserting the same doc_id twice keeps only one record
  edge cases          — empty collection returns [], ValueError on empty docs
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import patch

import chromadb
from chromadb.api import ClientAPI
import pytest

from src.models.schemas import RetrievalDocument, SourceType
from src.retrieval.store import (
    COLLECTION_NAME,
    QueryResult,
    get_collection,
    query_documents,
    upsert_documents,
)

# ── Constants ─────────────────────────────────────────────────────────────────

_DIM = 4  # tiny vectors are fine for unit tests


# ── Shared ephemeral client + per-test isolated collection name ───────────────

@pytest.fixture(scope="module")
def ephemeral() -> ClientAPI:
    """One EphemeralClient for the whole module. Tests are isolated
    by unique collection names (col_name fixture)."""
    return chromadb.EphemeralClient()


@pytest.fixture()
def col_name() -> str:
    """Return a fresh unique collection name for each test."""
    return f"test_{uuid.uuid4().hex[:8]}"


# ── Vector helpers ────────────────────────────────────────────────────────────

def _vec(seed: float) -> list[float]:
    import math
    raw = [seed, seed * 0.5, seed * 0.25, seed * 0.1]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


# ── Document factories ────────────────────────────────────────────────────────

def _story_doc(key: str = "AIP-2") -> RetrievalDocument:
    return RetrievalDocument(
        doc_id=f"{key}#story",
        source_type=SourceType.STORY,
        source_key=key,
        title="Customer Chat Warning Prompt",
        body="Customer Chat Warning Prompt | As a customer, I want a warning.",
        components=["Chat Widget"],
        labels=["security"],
        feature_area=None,
    )


def _bug_doc(key: str = "AIP-10") -> RetrievalDocument:
    return RetrievalDocument(
        doc_id=f"{key}#bug",
        source_type=SourceType.BUG,
        source_key=key,
        title="Chat input allows forbidden characters",
        body="Chat input allows forbidden characters | Click inside the chat input.",
        components=[],
        labels=[],
        feature_area=None,
    )


def _test_doc(key: str = "AIP-2", seq: int = 0) -> RetrievalDocument:
    return RetrievalDocument(
        doc_id=f"{key}#historical_test#{seq}",
        source_type=SourceType.HISTORICAL_TEST,
        source_key=key,
        title="Verify disclaimer appears",
        body="Verify disclaimer appears | Disclaimer is visible on chat open.",
        components=[],
        labels=[],
        feature_area="AC-1",
    )


def _note_doc(key: str = "AIP-2") -> RetrievalDocument:
    return RetrievalDocument(
        doc_id=f"{key}#qa_note#0",
        source_type=SourceType.QA_NOTE,
        source_key=key,
        title="Known linked defects",
        body="Known linked defects: AIP-10 — consider regression tests.",
        components=[],
        labels=[],
        feature_area=None,
    )


def _patch_embedder(docs_seed: float = 0.8, query_seed: float = 0.9):
    def fake_embed_documents(texts):
        return [_vec(docs_seed)] * len(texts)

    def fake_embed_query(text):
        return _vec(query_seed)

    return (
        patch("src.retrieval.store.embed_documents", side_effect=fake_embed_documents),
        patch("src.retrieval.store.embed_query",     side_effect=fake_embed_query),
    )


# ── get_collection ────────────────────────────────────────────────────────────

class TestGetCollection:
    def test_collection_name_is_correct(self, ephemeral: ClientAPI, col_name: str) -> None:
        col = get_collection(ephemeral, collection_name=col_name)
        assert col.name == col_name

    def test_idempotent_get_or_create(self, ephemeral: ClientAPI, col_name: str) -> None:
        col1 = get_collection(ephemeral, collection_name=col_name)
        col2 = get_collection(ephemeral, collection_name=col_name)
        assert col1.name == col2.name

    def test_cosine_distance_space(self, ephemeral: ClientAPI, col_name: str) -> None:
        col  = get_collection(ephemeral, collection_name=col_name)
        meta = col.metadata or {}
        assert meta.get("hnsw:space") == "cosine"


# ── upsert_documents ──────────────────────────────────────────────────────────

class TestUpsertDocuments:
    def test_returns_count_of_upserted_docs(self, ephemeral: ClientAPI, col_name: str) -> None:
        docs = [_story_doc(), _bug_doc()]
        p1, p2 = _patch_embedder()
        with p1, p2:
            n = upsert_documents(docs, client=ephemeral, collection_name=col_name)
        assert n == 2

    def test_collection_count_increases(self, ephemeral: ClientAPI, col_name: str) -> None:
        docs = [_story_doc(), _bug_doc(), _test_doc()]
        p1, p2 = _patch_embedder()
        with p1, p2:
            upsert_documents(docs, client=ephemeral, collection_name=col_name)
        col = get_collection(ephemeral, collection_name=col_name)
        assert col.count() == 3

    def test_idempotent_on_re_upsert(self, ephemeral: ClientAPI, col_name: str) -> None:
        docs = [_story_doc()]
        p1, p2 = _patch_embedder()
        with p1, p2:
            upsert_documents(docs, client=ephemeral, collection_name=col_name)
            upsert_documents(docs, client=ephemeral, collection_name=col_name)
        col = get_collection(ephemeral, collection_name=col_name)
        assert col.count() == 1

    def test_raises_value_error_for_empty_list(self, ephemeral: ClientAPI, col_name: str) -> None:
        with pytest.raises(ValueError, match="at least one document"):
            upsert_documents([], client=ephemeral, collection_name=col_name)

    def test_doc_ids_stored(self, ephemeral: ClientAPI, col_name: str) -> None:
        docs = [_story_doc("AIP-2"), _bug_doc("AIP-10")]
        p1, p2 = _patch_embedder()
        with p1, p2:
            upsert_documents(docs, client=ephemeral, collection_name=col_name)
        col    = get_collection(ephemeral, collection_name=col_name)
        result = col.get(ids=["AIP-2#story"])
        assert result["ids"] == ["AIP-2#story"]


# ── query_documents ───────────────────────────────────────────────────────────

class TestQueryDocuments:
    def _seed(self, ephemeral: ClientAPI, col_name: str, docs: list[RetrievalDocument]) -> None:
        p1, p2 = _patch_embedder()
        with p1, p2:
            upsert_documents(docs, client=ephemeral, collection_name=col_name)

    def test_returns_query_results(self, ephemeral: ClientAPI, col_name: str) -> None:
        self._seed(ephemeral, col_name, [_story_doc(), _bug_doc()])
        p1, p2 = _patch_embedder()
        with p1, p2:
            results = query_documents("chat warning", n_results=2, client=ephemeral, collection_name=col_name)
        assert len(results) == 2
        assert all(isinstance(r, QueryResult) for r in results)

    def test_respects_n_results(self, ephemeral: ClientAPI, col_name: str) -> None:
        self._seed(ephemeral, col_name, [_story_doc(), _bug_doc(), _test_doc(), _note_doc()])
        p1, p2 = _patch_embedder()
        with p1, p2:
            results = query_documents("disclaimer", n_results=2, client=ephemeral, collection_name=col_name)
        assert len(results) <= 2

    def test_returns_empty_list_for_empty_collection(self, ephemeral: ClientAPI, col_name: str) -> None:
        p1, p2 = _patch_embedder()
        with p1, p2:
            results = query_documents("anything", client=ephemeral, collection_name=col_name)
        assert results == []

    def test_result_has_correct_source_type(self, ephemeral: ClientAPI, col_name: str) -> None:
        self._seed(ephemeral, col_name, [_bug_doc()])
        p1, p2 = _patch_embedder()
        with p1, p2:
            results = query_documents("chat", n_results=1, client=ephemeral, collection_name=col_name)
        assert results[0].source_type == SourceType.BUG

    def test_result_body_non_empty(self, ephemeral: ClientAPI, col_name: str) -> None:
        self._seed(ephemeral, col_name, [_story_doc()])
        p1, p2 = _patch_embedder()
        with p1, p2:
            results = query_documents("chat", n_results=1, client=ephemeral, collection_name=col_name)
        assert results[0].body.strip()

    def test_score_is_one_minus_distance(self, ephemeral: ClientAPI, col_name: str) -> None:
        self._seed(ephemeral, col_name, [_story_doc()])
        p1, p2 = _patch_embedder()
        with p1, p2:
            results = query_documents("chat", n_results=1, client=ephemeral, collection_name=col_name)
        r = results[0]
        assert r.score == pytest.approx(1.0 - r.distance, abs=1e-5)

    def test_where_filter_by_source_type(self, ephemeral: ClientAPI, col_name: str) -> None:
        self._seed(ephemeral, col_name, [_story_doc(), _bug_doc(), _test_doc()])
        p1, p2 = _patch_embedder()
        with p1, p2:
            results = query_documents(
                "chat",
                n_results=5,
                where={"source_type": {"$eq": "bug"}},
                client=ephemeral,
                collection_name=col_name,
            )
        assert all(r.source_type == SourceType.BUG for r in results)
        assert len(results) == 1

    def test_where_filter_by_source_key(self, ephemeral: ClientAPI, col_name: str) -> None:
        self._seed(ephemeral, col_name, [_story_doc("AIP-2"), _story_doc("PROJ-1")])
        p1, p2 = _patch_embedder()
        with p1, p2:
            results = query_documents(
                "chat",
                n_results=5,
                where={"source_key": {"$eq": "AIP-2"}},
                client=ephemeral,
                collection_name=col_name,
            )
        assert all(r.source_key == "AIP-2" for r in results)


# ── Metadata round-trip ───────────────────────────────────────────────────────
# Use col.get() directly for deterministic lookup rather than query(), since
# all document vectors are identical (mocked) so ranking is arbitrary.

class TestMetadataRoundTrip:
    def _upsert_and_get(self, ephemeral: ClientAPI, col_name: str, doc: RetrievalDocument) -> dict:
        p1, p2 = _patch_embedder()
        with p1, p2:
            upsert_documents([doc], client=ephemeral, collection_name=col_name)
        col = get_collection(ephemeral, collection_name=col_name)
        raw = col.get(ids=[doc.doc_id], include=["metadatas"])
        assert raw["ids"] == [doc.doc_id]
        return raw["metadatas"][0]  # type: ignore[index]

    def test_components_stored_as_json(self, ephemeral: ClientAPI, col_name: str) -> None:
        doc  = _story_doc()
        meta = self._upsert_and_get(ephemeral, col_name, doc)
        assert json.loads(meta["components"]) == ["Chat Widget"]

    def test_labels_stored_as_json(self, ephemeral: ClientAPI, col_name: str) -> None:
        doc  = _story_doc()
        meta = self._upsert_and_get(ephemeral, col_name, doc)
        assert json.loads(meta["labels"]) == ["security"]

    def test_feature_area_stored(self, ephemeral: ClientAPI, col_name: str) -> None:
        doc  = _test_doc()
        meta = self._upsert_and_get(ephemeral, col_name, doc)
        assert meta["feature_area"] == "AC-1"

    def test_none_feature_area_stored_as_empty_string(self, ephemeral: ClientAPI, col_name: str) -> None:
        doc  = _story_doc()
        meta = self._upsert_and_get(ephemeral, col_name, doc)
        assert meta["feature_area"] == ""

    def test_source_type_stored_as_string(self, ephemeral: ClientAPI, col_name: str) -> None:
        doc  = _bug_doc()
        meta = self._upsert_and_get(ephemeral, col_name, doc)
        assert meta["source_type"] == "bug"

    def test_title_stored(self, ephemeral: ClientAPI, col_name: str) -> None:
        doc  = _story_doc()
        meta = self._upsert_and_get(ephemeral, col_name, doc)
        assert meta["title"] == "Customer Chat Warning Prompt"

    def test_query_result_deserialises_components(self, ephemeral: ClientAPI, col_name: str) -> None:
        doc = _story_doc()
        p1, p2 = _patch_embedder()
        with p1, p2:
            upsert_documents([doc], client=ephemeral, collection_name=col_name)
            results = query_documents(
                "chat",
                n_results=1,
                where={"source_type": {"$eq": "story"}},
                client=ephemeral,
                collection_name=col_name,
            )
        assert results[0].components == ["Chat Widget"]

    def test_query_result_deserialises_labels(self, ephemeral: ClientAPI, col_name: str) -> None:
        doc = _story_doc()
        p1, p2 = _patch_embedder()
        with p1, p2:
            upsert_documents([doc], client=ephemeral, collection_name=col_name)
            results = query_documents(
                "chat",
                n_results=1,
                where={"source_type": {"$eq": "story"}},
                client=ephemeral,
                collection_name=col_name,
            )
        assert results[0].labels == ["security"]

    def test_query_result_feature_area_none_when_empty(self, ephemeral: ClientAPI, col_name: str) -> None:
        doc = _story_doc()
        p1, p2 = _patch_embedder()
        with p1, p2:
            upsert_documents([doc], client=ephemeral, collection_name=col_name)
            results = query_documents(
                "chat",
                n_results=1,
                where={"source_type": {"$eq": "story"}},
                client=ephemeral,
                collection_name=col_name,
            )
        assert results[0].feature_area is None

    def test_query_result_source_type_is_enum(self, ephemeral: ClientAPI, col_name: str) -> None:
        doc = _bug_doc()
        p1, p2 = _patch_embedder()
        with p1, p2:
            upsert_documents([doc], client=ephemeral, collection_name=col_name)
            results = query_documents(
                "bug",
                n_results=1,
                where={"source_type": {"$eq": "bug"}},
                client=ephemeral,
                collection_name=col_name,
            )
        assert results[0].source_type == SourceType.BUG
