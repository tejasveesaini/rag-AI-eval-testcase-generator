"""Tests for src/retrieval/embedder.py.

All tests mock the google-genai client — no real API calls are made.
We verify:
  - embed_documents() calls embed_content with task_type=RETRIEVAL_DOCUMENT
  - embed_query()     calls embed_content with task_type=RETRIEVAL_QUERY
  - batching splits at _BATCH_SIZE boundaries
  - EmbeddingError is raised for empty / malformed API responses
  - ValueError is raised for empty input
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from src.retrieval.embedder import EmbeddingError, embed_documents, embed_query

# ── Helpers ───────────────────────────────────────────────────────────────────

_DIM = 768  # dimensionality of text-embedding-004


def _make_embedding(value: float = 0.1) -> MagicMock:
    """Return a mock ContentEmbedding with a non-empty .values list."""
    emb = MagicMock()
    emb.values = [value] * _DIM
    return emb


def _make_response(*values: float) -> MagicMock:
    """Return a mock EmbedContentResponse with one embedding per value."""
    resp = MagicMock()
    resp.embeddings = [_make_embedding(v) for v in values]
    return resp


def _patch_client(response: MagicMock):
    """Patch genai.Client so embed_content returns response."""
    mock_client = MagicMock()
    mock_client.models.embed_content.return_value = response
    return patch("src.retrieval.embedder.genai.Client", return_value=mock_client)


# ── embed_documents ───────────────────────────────────────────────────────────

class TestEmbedDocuments:
    def test_returns_one_vector_per_text(self) -> None:
        texts    = ["hello", "world"]
        response = _make_response(0.1, 0.2)
        with _patch_client(response):
            vecs = embed_documents(texts)
        assert len(vecs) == 2

    def test_vector_has_correct_dimensionality(self) -> None:
        response = _make_response(0.5)
        with _patch_client(response):
            vecs = embed_documents(["test text"])
        assert len(vecs[0]) == _DIM

    def test_task_type_is_retrieval_document(self) -> None:
        response = _make_response(0.1)
        mock_client = MagicMock()
        mock_client.models.embed_content.return_value = response
        with patch("src.retrieval.embedder.genai.Client", return_value=mock_client):
            embed_documents(["some doc"])
        _, kwargs = mock_client.models.embed_content.call_args
        assert kwargs["config"].task_type == "RETRIEVAL_DOCUMENT"

    def test_uses_model_from_settings(self) -> None:
        response = _make_response(0.1)
        mock_client = MagicMock()
        mock_client.models.embed_content.return_value = response
        with patch("src.retrieval.embedder.genai.Client", return_value=mock_client):
            embed_documents(["doc"])
        _, kwargs = mock_client.models.embed_content.call_args
        from src.config import get_settings
        assert kwargs["model"] == get_settings().embedding_model

    def test_batching_makes_multiple_calls(self) -> None:
        """101 texts should require 2 API calls: batch of 100 + batch of 1."""
        texts   = [f"text {i}" for i in range(101)]
        # First call returns 100 embeddings, second returns 1
        resp100 = MagicMock()
        resp100.embeddings = [_make_embedding(0.1) for _ in range(100)]
        resp1   = MagicMock()
        resp1.embeddings = [_make_embedding(0.2)]

        mock_client = MagicMock()
        mock_client.models.embed_content.side_effect = [resp100, resp1]
        with patch("src.retrieval.embedder.genai.Client", return_value=mock_client):
            vecs = embed_documents(texts)

        assert mock_client.models.embed_content.call_count == 2
        assert len(vecs) == 101

    def test_raises_value_error_for_empty_input(self) -> None:
        with pytest.raises(ValueError, match="at least one text"):
            embed_documents([])

    def test_raises_embedding_error_when_api_returns_no_embeddings(self) -> None:
        resp = MagicMock()
        resp.embeddings = None
        with _patch_client(resp):
            with pytest.raises(EmbeddingError, match="no embeddings"):
                embed_documents(["text"])

    def test_raises_embedding_error_when_vector_is_empty(self) -> None:
        resp = MagicMock()
        bad_emb = MagicMock()
        bad_emb.values = []
        resp.embeddings = [bad_emb]
        with _patch_client(resp):
            with pytest.raises(EmbeddingError, match="empty vector"):
                embed_documents(["text"])

    def test_preserves_order(self) -> None:
        """Vectors are returned in the same order as input texts."""
        resp = _make_response(0.1, 0.9)
        with _patch_client(resp):
            vecs = embed_documents(["a", "b"])
        assert vecs[0][0] == pytest.approx(0.1)
        assert vecs[1][0] == pytest.approx(0.9)


# ── embed_query ───────────────────────────────────────────────────────────────

class TestEmbedQuery:
    def test_returns_single_vector(self) -> None:
        response = _make_response(0.3)
        with _patch_client(response):
            vec = embed_query("what is the login flow?")
        assert isinstance(vec, list)
        assert len(vec) == _DIM

    def test_task_type_is_retrieval_query(self) -> None:
        response = _make_response(0.3)
        mock_client = MagicMock()
        mock_client.models.embed_content.return_value = response
        with patch("src.retrieval.embedder.genai.Client", return_value=mock_client):
            embed_query("test query")
        _, kwargs = mock_client.models.embed_content.call_args
        assert kwargs["config"].task_type == "RETRIEVAL_QUERY"

    def test_raises_value_error_for_empty_string(self) -> None:
        with pytest.raises(ValueError, match="non-empty text"):
            embed_query("")

    def test_raises_value_error_for_whitespace_only(self) -> None:
        with pytest.raises(ValueError, match="non-empty text"):
            embed_query("   ")

    def test_raises_embedding_error_when_api_returns_no_embeddings(self) -> None:
        resp = MagicMock()
        resp.embeddings = None
        with _patch_client(resp):
            with pytest.raises(EmbeddingError):
                embed_query("query")

    def test_single_api_call_regardless_of_text_length(self) -> None:
        response = _make_response(0.5)
        mock_client = MagicMock()
        mock_client.models.embed_content.return_value = response
        with patch("src.retrieval.embedder.genai.Client", return_value=mock_client):
            embed_query("a very long query " * 50)
        assert mock_client.models.embed_content.call_count == 1
