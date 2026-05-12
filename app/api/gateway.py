"""
POST /api/v1/chat — LLM Inference Gateway endpoint.

Handles both standard (JSON) and streaming (SSE) responses.
Streaming is PREMIUM-only.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.gateway.router import route
from app.gateway.streaming import build_streaming_response
from app.orchestrator.pipeline import run_inference
from app.utils.logger import get_logger

logger = get_logger("api.gateway")

router = APIRouter()


# ── Request / Response schemas ────────────────────────────────────────────────

class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    prompt: str = Field(..., description="The user's prompt text")
    messages: List[Message] = Field(
        default_factory=list,
        description="Conversation history in OpenAI format",
    )
    stream: bool = Field(False, description="Enable SSE streaming (PREMIUM only)")
    user_tier: Literal["free", "premium", "agent"] = Field(
        "free",
        description="User tier — 'agent' is internal, skips critic+cache",
    )


class ChatResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    response: str
    model_used: str
    quality_score: float
    iterations: int
    cached: bool
    estimated_cost_usd: float
    note: Optional[str] = None


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/chat", response_model=ChatResponse, summary="LLM Inference Gateway")
async def chat(
    request: ChatRequest,
    db: AsyncSession = Depends(get_db),
) -> ChatResponse | StreamingResponse:
    """
    Tiered LLM inference endpoint.

    - FREE tier: single-pass Groq + one critic evaluation
    - PREMIUM tier: iterative Claude + Groq critic loop (up to 5 rounds)
    - Streaming (SSE) available for PREMIUM only
    """

    # ── Streaming path (PREMIUM only) ─────────────────────────────────────────
    if request.stream:
        if request.user_tier != "premium":
            raise HTTPException(
                status_code=403,
                detail="Streaming is only available for PREMIUM users.",
            )
        decision = route(request.user_tier)
        messages = [m.model_dump() for m in request.messages]
        if not messages:
            messages = [{"role": "user", "content": request.prompt}]
        return build_streaming_response(model=decision["model"], messages=messages)

    # ── Standard inference path ───────────────────────────────────────────────
    messages = [m.model_dump() for m in request.messages]
    if not messages:
        # If no history provided, treat prompt as the sole user message
        messages = [{"role": "user", "content": request.prompt}]

    try:
        result = await run_inference(
            prompt=request.prompt,
            messages=messages,
            user_tier=request.user_tier,
            db=db,
        )
    except Exception as exc:
        logger.error("Inference pipeline error: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=503,
            detail=f"LLM service unavailable: {exc}",
        )

    return ChatResponse(
        response=result.response,
        model_used=result.model_used,
        quality_score=result.quality_score,
        iterations=result.iterations,
        cached=result.cached,
        estimated_cost_usd=result.estimated_cost_usd,
        note=result.note,
    )
