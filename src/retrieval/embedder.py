"""Gemini embedding wrapper.

Single responsibility: turn text into float vectors using the Gemini
Embeddings API (text-embedding-004, 768 dimensions).

Two task types are exposed so callers can never mix them accidentally:
  embed_documents(texts)  →  task_type=RETRIEVAL_DOCUMENT  (indexing)
  embed_query(text)       →  task_type=RETRIEVAL_QUERY      (searching)

Using the correct task type is important: text-embedding-004 is trained
with separate objective heads for documents vs queries, so mixing them
silently degrades retrieval quality.

Public API
----------
  embed_documents(texts: list[str]) -> list[list[float]]
  embed_query(text: str)            -> list[float]

Both functions are synchronous (google-genai sync client) and raise
EmbeddingError on any API failure so callers get a single exception type
to handle.

Rate-limit note
---------------
Gemini Embeddings allows up to 100 texts per batch request.  embed_documents
automatically splits the input into batches of _BATCH_SIZE (default 100)
and concatenates the results so callers never need to think about this.
"""

from __future__ import annotations

import logging
from typing import Sequence

from google import genai
from google.genai import types

from src.config import get_settings

logger = logging.getLogger(__name__)

# Maximum texts per single embed_content call (API limit is 100)
_BATCH_SIZE = 100


class EmbeddingError(RuntimeError):
    """Raised when the Gemini Embeddings API returns an error or empty result."""


def _get_client() -> genai.Client:
    settings = get_settings()
    return genai.Client(api_key=settings.gemini_api_key.get_secret_value())


def _call_embed(
    client: genai.Client,
    model: str,
    texts: list[str],
    task_type: str,
) -> list[list[float]]:
    """Make one embed_content call and return the float vectors.

    Args:
        client:    Authenticated genai.Client.
        model:     Embedding model name, e.g. "models/text-embedding-004".
        texts:     Batch of texts to embed (≤ _BATCH_SIZE).
        task_type: "RETRIEVAL_DOCUMENT" or "RETRIEVAL_QUERY".

    Returns:
        List of float vectors, one per input text.

    Raises:
        EmbeddingError: if the API returns no embeddings or fewer than expected.
    """
    response = client.models.embed_content(
        model=model,
        contents=texts,
        config=types.EmbedContentConfig(task_type=task_type),
    )

    if not response.embeddings:
        raise EmbeddingError(
            f"Gemini returned no embeddings for task_type={task_type!r}. "
            f"Model: {model!r}. Input count: {len(texts)}."
        )

    vectors = [emb.values for emb in response.embeddings]

    # Guard: every embedding must be a non-empty float list
    for i, vec in enumerate(vectors):
        if not vec:
            raise EmbeddingError(
                f"Gemini returned an empty vector for text[{i}] "
                f"(task_type={task_type!r})."
            )

    return vectors  # type: ignore[return-value]  # vec is list[float] | None, guarded above


def embed_documents(texts: Sequence[str]) -> list[list[float]]:
    """Embed a list of texts for indexing (RETRIEVAL_DOCUMENT task type).

    Automatically batches into groups of _BATCH_SIZE.

    Args:
        texts: One or more document bodies to embed.

    Returns:
        A list of 768-dimensional float vectors, one per input text,
        in the same order as the input.

    Raises:
        EmbeddingError: on any API or parsing failure.
        ValueError:     if texts is empty.
    """
    if not texts:
        raise ValueError("embed_documents requires at least one text.")

    settings  = get_settings()
    client    = _get_client()
    model     = settings.embedding_model
    all_vecs: list[list[float]] = []

    text_list = list(texts)
    for start in range(0, len(text_list), _BATCH_SIZE):
        batch = text_list[start : start + _BATCH_SIZE]
        logger.debug("Embedding %d document(s) [%d–%d]", len(batch), start, start + len(batch) - 1)
        vecs = _call_embed(client, model, batch, task_type="RETRIEVAL_DOCUMENT")
        all_vecs.extend(vecs)

    return all_vecs


def embed_query(text: str) -> list[float]:
    """Embed a single query string for similarity search (RETRIEVAL_QUERY task type).

    Args:
        text: The query text to embed.

    Returns:
        A 768-dimensional float vector.

    Raises:
        EmbeddingError: on any API or parsing failure.
        ValueError:     if text is blank.
    """
    if not text or not text.strip():
        raise ValueError("embed_query requires non-empty text.")

    settings = get_settings()
    client   = _get_client()
    model    = settings.embedding_model

    logger.debug("Embedding query: %s…", text[:60])
    vecs = _call_embed(client, model, [text], task_type="RETRIEVAL_QUERY")
    return vecs[0]
