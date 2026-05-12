"""
Token usage tracker — records every LLM call (including critic calls) to the
token_usage table and calculates estimated cost in USD.

Pricing (per 1M tokens):
  groq/llama-3.1-8b-instant : $0.05 input  / $0.08 output
  claude-sonnet-4-20250514  : $3.00 input  / $15.00 output
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.logger import get_logger

logger = get_logger("token_tracker")

# Cost per 1M tokens in USD
_PRICING: dict[str, dict[str, float]] = {
    "llama-3.1-8b-instant": {"input": 0.05, "output": 0.08},
    "groq/llama-3.1-8b-instant": {"input": 0.05, "output": 0.08},
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
}

_DEFAULT_PRICING = {"input": 0.0, "output": 0.0}


def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return estimated cost in USD for the given token counts."""
    pricing = _PRICING.get(model, _DEFAULT_PRICING)
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    return round(input_cost + output_cost, 8)


async def record_usage(
    model_used: str,
    input_tokens: int,
    output_tokens: int,
    db: AsyncSession,
    user_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> float:
    """
    Persist a token-usage record and return the estimated cost.

    This is async and non-blocking — it does not slow down the response path
    because it is awaited after the response is ready to return.
    """
    cost = calculate_cost(model_used, input_tokens, output_tokens)

    try:
        await db.execute(
            text(
                """
                INSERT INTO token_usage
                    (id, user_id, model_used, input_tokens, output_tokens, estimated_cost_usd, task_id)
                VALUES
                    (:id, :user_id, :model_used, :input_tokens, :output_tokens, :estimated_cost_usd, :task_id)
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "model_used": model_used,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated_cost_usd": cost,
                "task_id": task_id,
            },
        )
        await db.commit()
    except Exception as exc:
        logger.warning("Failed to record token usage (non-fatal): %s", exc)
        await db.rollback()

    return cost
