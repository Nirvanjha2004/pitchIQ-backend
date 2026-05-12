"""Redis-based rate limiting.

Works with both local Redis (redis://) and Upstash (rediss://).
"""

from __future__ import annotations

from typing import Tuple

import redis

from app.config import settings


def _make_redis_client() -> redis.Redis:
    url = settings.REDIS_URL
    if url.startswith("rediss://"):
        return redis.from_url(url, ssl_cert_reqs=None)
    return redis.from_url(url)


class RateLimiter:
    """Sliding-window rate limiter backed by Redis."""

    def __init__(self):
        self.redis_client = _make_redis_client()
        self.default_limit = 100   # requests per window
        self.default_window = 3600  # 1 hour

    def is_allowed(
        self,
        user_id: str,
        limit: int = None,
        window: int = None,
    ) -> Tuple[bool, dict]:
        """Return (allowed, metadata). Fails open if Redis is unavailable."""
        limit = limit or self.default_limit
        window = window or self.default_window
        key = f"rate_limit:{user_id}"

        try:
            current = self.redis_client.incr(key)
            if current == 1:
                self.redis_client.expire(key, window)

            remaining = max(0, limit - current)
            return current <= limit, {
                "limit": limit,
                "remaining": remaining,
                "reset_at": self.redis_client.ttl(key),
            }
        except Exception as exc:
            # Fail open — don't block requests if Redis is down
            return True, {"limit": limit, "error": str(exc)}

    def reset(self, user_id: str) -> bool:
        """Delete the rate-limit key for a user."""
        self.redis_client.delete(f"rate_limit:{user_id}")
        return True
