"""
Task event emitter — pushes SSE events to a Redis list.

Key:      task_events:{task_id}
Values:   JSON-encoded event dicts
Sentinel: "DONE" string pushed when pipeline finishes
TTL:      1 hour

Works with both local Redis (redis://) and Upstash (rediss://).
The rediss:// scheme enables TLS automatically via the redis-py client.
"""

from __future__ import annotations

import json
import ssl
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import redis as redis_sync

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger("events")

_redis: Optional[redis_sync.Redis] = None


def _get_redis() -> redis_sync.Redis:
    global _redis
    if _redis is None:
        _redis = _make_redis_client()
    return _redis


def _make_redis_client() -> redis_sync.Redis:
    """
    Build a redis-py client from REDIS_URL.

    For Upstash (rediss://): redis-py enables TLS automatically when the scheme
    is rediss://. We pass ssl_cert_reqs=None so it doesn't try to verify
    Upstash's certificate against the system CA bundle (which often fails in
    Docker containers with minimal CA stores).
    """
    url = settings.REDIS_URL
    if url.startswith("rediss://"):
        return redis_sync.from_url(
            url,
            decode_responses=False,
            ssl_cert_reqs=None,          # skip cert verification for Upstash
        )
    return redis_sync.from_url(url, decode_responses=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _push(task_id: str, event: Dict[str, Any]) -> None:
    """Push a JSON event to the Redis list. Silently ignores errors."""
    try:
        key = f"task_events:{task_id}"
        _get_redis().rpush(key, json.dumps(event))
        _get_redis().expire(key, 3600)
    except Exception as exc:
        logger.warning("Event push failed (non-fatal): %s", exc)


def emit_task_started(task_id: str, message: str = "Task received. Analyzing...") -> None:
    _push(task_id, {"type": "task_started", "message": message, "timestamp": _now()})


def emit_plan_ready(task_id: str, agents: List[str]) -> None:
    agent_str = " → ".join(agents)
    _push(task_id, {
        "type": "plan_ready",
        "message": f"Plan created. Agents required: {agent_str}",
        "agents": agents,
        "timestamp": _now(),
    })


def emit_agent_started(task_id: str, agent: str, message: str) -> None:
    _push(task_id, {
        "type": "agent_started",
        "agent": agent,
        "message": message,
        "timestamp": _now(),
    })


def emit_agent_log(task_id: str, agent: str, message: str) -> None:
    _push(task_id, {
        "type": "agent_log",
        "agent": agent,
        "message": message,
        "timestamp": _now(),
    })


def emit_agent_completed(
    task_id: str,
    agent: str,
    message: str,
    tokens: int = 0,
    cost_usd: float = 0.0,
    latency_ms: int = 0,
) -> None:
    _push(task_id, {
        "type": "agent_completed",
        "agent": agent,
        "message": message,
        "tokens": tokens,
        "cost_usd": cost_usd,
        "latency_ms": latency_ms,
        "timestamp": _now(),
    })


def emit_task_completed(
    task_id: str,
    total_cost_usd: float,
    total_tokens: int,
    critic_score: float,
) -> None:
    score_str = f"{critic_score:.1f}/10" if critic_score > 0 else "n/a"
    _push(task_id, {
        "type": "task_completed",
        "message": f"All agents completed — ${total_cost_usd:.4f} · {score_str}",
        "total_cost_usd": total_cost_usd,
        "total_tokens": total_tokens,
        "critic_score": critic_score,
        "timestamp": _now(),
    })
    try:
        _get_redis().rpush(f"task_events:{task_id}", "DONE")
    except Exception:
        pass


def emit_task_failed(task_id: str, message: str) -> None:
    _push(task_id, {
        "type": "task_failed",
        "message": message,
        "timestamp": _now(),
    })
    try:
        _get_redis().rpush(f"task_events:{task_id}", "DONE")
    except Exception:
        pass
