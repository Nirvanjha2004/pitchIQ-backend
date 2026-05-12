"""
Model Router — maps user tier to inference strategy and model.

Tier alone determines routing; no complexity classification needed.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from app.config import settings


class RouteDecision(TypedDict):
    strategy: Literal["single_pass", "iterative", "agent_direct"]
    model: str


def route(user_tier: str) -> RouteDecision:
    """
    Return the inference strategy and model for the given tier.

    free    → single_pass    Groq, 1 LLM call + 1 critic + cache
    premium → iterative      Groq/Claude, up to MAX_CRITIC_ITERATIONS loops + cache
    agent   → agent_direct   Groq, 1 LLM call only — NO critic, NO cache
                             Used by ALL agents (planner, researcher, enricher, writer).
                             Quality is handled by the orchestrator's CriticAgent, not
                             the gateway loop. Using "premium" tier for writer caused
                             the iteration loop to restart on each HTTP retry.
    """
    if user_tier == "premium":
        return RouteDecision(strategy="iterative", model=settings.PREMIUM_MODEL)
    if user_tier == "agent":
        return RouteDecision(strategy="agent_direct", model=settings.GROQ_CHEAP_MODEL)
    # default: free
    return RouteDecision(strategy="single_pass", model=settings.GROQ_CHEAP_MODEL)
