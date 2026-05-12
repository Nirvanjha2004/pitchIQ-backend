"""
Semantic cache backed by pgvector.

Table: prompt_cache
Similarity threshold: 0.92 (cosine)
Embedding model: jina-embeddings-v3 via Jina AI API (1024-dim)
Endpoint: https://api.jina.ai/v1/embeddings
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Optional

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger("cache")

_JINA_ENDPOINT = "https://api.jina.ai/v1/embeddings"
_JINA_MODEL = "jina-embeddings-v3"
_JINA_DIMENSIONS = 1024
# text-matching is the correct task for semantic cache (same text → same space)
# retrieval.query/passage are asymmetric and break same-prompt lookups
_TASK_CACHE = "text-matching"


async def _embed(text_input: str, task: str) -> list[float]:
    """
    Call the Jina AI embeddings API and return a 1024-dim vector.
    Raises httpx.HTTPError on failure — callers should catch and handle.
    Retries once on connection/DNS errors.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.JINA_API_KEY}",
    }
    payload = {
        "input": [text_input],
        "model": _JINA_MODEL,
        "dimensions": _JINA_DIMENSIONS,
        "task": task,
    }
    last_exc = None
    for attempt in range(2):  # 1 retry on transient connection/timeout errors
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(_JINA_ENDPOINT, headers=headers, json=payload)
                response.raise_for_status()
                return response.json()["data"][0]["embedding"]
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TimeoutException) as exc:
            last_exc = exc
            logger.warning("Jina embed attempt %d failed (%s), retrying...", attempt + 1, exc)
            await asyncio.sleep(1.0)
        except Exception:
            raise
    raise last_exc


class CacheResult:
    def __init__(self, response_text: str, model_used: str, quality_score: float):
        self.response_text = response_text
        self.model_used = model_used
        self.quality_score = quality_score


async def cache_get(prompt: str, db: AsyncSession) -> Optional[CacheResult]:
    """
    Look up a semantically similar prompt in the cache.

    Returns CacheResult on hit (cosine similarity >= 0.92), None on miss.
    Swallows all errors so the pipeline never crashes on cache failure.
    """
    if not settings.CACHE_ENABLED:
        return None

    try:
        embedding = await _embed(prompt, task=_TASK_CACHE)
        embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"

        # pgvector cosine distance = 1 - cosine_similarity
        # distance < (1 - threshold)  ↔  similarity > threshold
        distance_threshold = 1.0 - settings.CACHE_SIMILARITY_THRESHOLD

        result = await db.execute(
            text(
                """
                SELECT response_text, model_used, quality_score
                FROM prompt_cache
                WHERE embedding <=> CAST(:emb AS vector) < :dist
                ORDER BY embedding <=> CAST(:emb AS vector)
                LIMIT 1
                """
            ),
            {"emb": embedding_str, "dist": distance_threshold},
        )
        row = result.fetchone()
        if row:
            logger.info("Cache HIT  — prompt[:60]: %s", prompt[:60])
            return CacheResult(
                response_text=row[0],
                model_used=row[1],
                quality_score=row[2] or 0.0,
            )

        logger.info("Cache MISS — prompt[:60]: %s", prompt[:60])
        return None

    except Exception as exc:
        logger.warning("Cache read failed (continuing without cache): %s", exc)
        return None


async def cache_set(
    prompt: str,
    response_text: str,
    model_used: str,
    quality_score: float,
    db: AsyncSession,
) -> None:
    """
    Store a prompt + response in the semantic cache.
    Swallows all errors so the pipeline never crashes on cache write failure.
    """
    if not settings.CACHE_ENABLED:
        return

    try:
        embedding = await _embed(prompt, task=_TASK_CACHE)
        embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"

        await db.execute(
            text(
                """
                INSERT INTO prompt_cache
                    (id, prompt_text, embedding, response_text, model_used, quality_score)
                VALUES
                    (:id, :prompt_text, CAST(:embedding AS vector), :response_text, :model_used, :quality_score)
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "prompt_text": prompt,
                "embedding": embedding_str,
                "response_text": response_text,
                "model_used": model_used,
                "quality_score": quality_score,
            },
        )
        await db.commit()
        logger.info("Cached response — prompt[:60]: %s", prompt[:60])

    except Exception as exc:
        logger.warning("Cache write failed (non-fatal): %s", exc)
        await db.rollback()
