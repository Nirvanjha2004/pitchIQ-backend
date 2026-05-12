"""
BaseAgent — all agents extend this class.

Key design rules:
- All LLM calls go through the gateway at POST /api/v1/chat
- Never import anthropic or groq directly
- Retry logic: 3 attempts with exponential backoff
- Every call logs to token_usage via the gateway response metadata
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger("agent")

# Gateway endpoint — all LLM calls go here
_GATEWAY_URL = "http://localhost:8000/api/v1/chat"

# Retry config
_MAX_RETRIES = 3
_BACKOFF_BASE = 2.0  # seconds


class GatewayResponse:
    """Normalized response from the gateway."""

    def __init__(self, data: Dict[str, Any]):
        self.response = data.get("response", "")
        self.model_used = data.get("model_used", "")
        self.quality_score = data.get("quality_score", 0.0)
        self.iterations = data.get("iterations", 1)
        self.cached = data.get("cached", False)
        self.estimated_cost_usd = data.get("estimated_cost_usd", 0.0)
        self.note = data.get("note")


class BaseAgent(ABC):
    """
    Base class for all PitchIQ agents.

    Subclasses must implement execute(task, context) → dict.
    All LLM calls must go through _call_gateway().
    """

    def __init__(self, name: str, role: str, tier: str = "free"):
        self.name = name
        self.role = role
        self.tier = tier  # "free" or "premium"
        self._total_cost = 0.0
        self._total_tokens = 0

    @abstractmethod
    async def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute the agent's task.

        Args:
            task:    Instruction from the planner for this agent.
            context: All previous agent outputs from shared state.

        Returns:
            Dict with agent's output — structure varies per agent.
        """
        pass

    async def _call_gateway(
        self,
        prompt: str,
        messages: Optional[List[Dict[str, str]]] = None,
        tier_override: Optional[str] = None,
        skip_cache: bool = False,
    ) -> GatewayResponse:
        """
        Call the gateway LLM endpoint with retry + exponential backoff.

        Args:
            prompt:        The prompt text.
            messages:      Optional conversation history.
            tier_override: Override the agent's default tier for this call.
            skip_cache:    If True, adds a unique suffix to bust the semantic cache.
                           Use for planning calls that must always be fresh.

        Returns:
            GatewayResponse with text and metadata.

        Raises:
            RuntimeError if all retries exhausted.
        """
        import uuid as _uuid
        tier = tier_override or self.tier

        # Bust cache by making the prompt unique — planner must never get a stale plan
        effective_prompt = prompt
        if skip_cache:
            effective_prompt = f"{prompt} [task_id:{_uuid.uuid4().hex[:8]}]"

        payload = {
            "prompt": effective_prompt,
            "messages": messages or [{"role": "user", "content": prompt}],
            "stream": False,
            "user_tier": tier,
        }

        last_exc: Optional[Exception] = None

        for attempt in range(1, _MAX_RETRIES + 1):
            start_ms = time.monotonic() * 1000
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    resp = await client.post(_GATEWAY_URL, json=payload)
                    resp.raise_for_status()
                    data = resp.json()

                latency_ms = int(time.monotonic() * 1000 - start_ms)
                gw = GatewayResponse(data)

                self._total_cost += gw.estimated_cost_usd
                self._total_tokens += 0  # tokens tracked inside gateway

                logger.info(
                    "[%s] gateway call OK — latency=%dms cost=$%.5f cached=%s",
                    self.name,
                    latency_ms,
                    gw.estimated_cost_usd,
                    gw.cached,
                )
                return gw

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = exc
                wait = _BACKOFF_BASE ** attempt
                logger.warning(
                    "[%s] gateway attempt %d/%d failed (%s), retrying in %.1fs",
                    self.name, attempt, _MAX_RETRIES, exc, wait,
                )
                await asyncio.sleep(wait)

            except httpx.HTTPStatusError as exc:
                last_exc = exc
                # 503 = LLM unavailable, worth retrying; 4xx = don't retry
                if exc.response.status_code < 500:
                    raise RuntimeError(
                        f"[{self.name}] Gateway returned {exc.response.status_code}: "
                        f"{exc.response.text[:200]}"
                    ) from exc
                wait = _BACKOFF_BASE ** attempt
                logger.warning(
                    "[%s] gateway attempt %d/%d returned %d, retrying in %.1fs",
                    self.name, attempt, _MAX_RETRIES,
                    exc.response.status_code, wait,
                )
                await asyncio.sleep(wait)

        raise RuntimeError(
            f"[{self.name}] Gateway failed after {_MAX_RETRIES} attempts: {last_exc}"
        )

    @property
    def total_cost(self) -> float:
        return self._total_cost

    @property
    def total_tokens(self) -> int:
        return self._total_tokens
