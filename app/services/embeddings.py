"""
Text-to-vector embeddings via Jina AI API.

Model: jina-embeddings-v3 (1024-dim, multilingual, 8K context)
Endpoint: https://api.jina.ai/v1/embeddings

The gateway's cache.py calls the Jina API directly.
This module is kept for agent-layer use cases that need embeddings
outside the cache (e.g. similarity search in research agent).
"""

from __future__ import annotations

from typing import List

import httpx

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger("embeddings")

_JINA_ENDPOINT = "https://api.jina.ai/v1/embeddings"
_JINA_MODEL = "jina-embeddings-v3"
_JINA_DIMENSIONS = 1024


class EmbeddingsService:
    """Generate embeddings via Jina AI API."""

    async def embed_text(self, text: str, task: str = "text-matching") -> List[float]:
        """Convert a single text to a 1024-dim embedding vector."""
        return (await self.embed_batch([text], task=task))[0]

    async def embed_batch(
        self, texts: List[str], task: str = "text-matching"
    ) -> List[List[float]]:
        """Convert multiple texts to embeddings in a single API call."""
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.JINA_API_KEY}",
        }
        payload = {
            "input": texts,
            "model": _JINA_MODEL,
            "dimensions": _JINA_DIMENSIONS,
            "task": task,
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(_JINA_ENDPOINT, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()["data"]
            # API returns items sorted by index
            return [item["embedding"] for item in sorted(data, key=lambda x: x["index"])]

    @staticmethod
    def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
        """Calculate cosine similarity between two vectors."""
        dot = sum(a * b for a, b in zip(vec1, vec2))
        mag1 = sum(a ** 2 for a in vec1) ** 0.5
        mag2 = sum(b ** 2 for b in vec2) ** 0.5
        if mag1 == 0 or mag2 == 0:
            return 0.0
        return dot / (mag1 * mag2)
