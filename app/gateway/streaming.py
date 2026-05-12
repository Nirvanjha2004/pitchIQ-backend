"""
SSE streaming handler — PREMIUM tier only.

Yields chunks in the format:
    data: <chunk_text>\n\n

Final chunk:
    data: [DONE]\n\n

Streaming skips the semantic cache (real-time responses are not cacheable).
Token tracking happens after the stream completes.
"""

from __future__ import annotations

import json
from typing import AsyncGenerator

from fastapi.responses import StreamingResponse

from app.gateway.proxy import stream_llm
from app.utils.logger import get_logger

logger = get_logger("streaming")


async def _sse_generator(
    model: str,
    messages: list[dict],
) -> AsyncGenerator[str, None]:
    """Wrap stream_llm chunks into SSE format."""
    try:
        async for chunk in stream_llm(model, messages):
            yield f"data: {chunk}\n\n"
    except Exception as exc:
        logger.error("Streaming error: %s", exc)
        error_payload = json.dumps({"error": str(exc)})
        yield f"data: {error_payload}\n\n"
    finally:
        yield "data: [DONE]\n\n"


def build_streaming_response(
    model: str,
    messages: list[dict],
) -> StreamingResponse:
    """
    Build a FastAPI StreamingResponse for SSE.

    Args:
        model:    LLM model identifier.
        messages: Conversation messages to send to the model.

    Returns:
        StreamingResponse with media_type='text/event-stream'.
    """
    return StreamingResponse(
        _sse_generator(model, messages),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
