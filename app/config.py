"""Configuration and environment variables for PitchIQ"""

from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    # Database — Supabase Transaction pooler
    # Format: postgresql+asyncpg://postgres.[ref]:[pass]@aws-0-[region].pooler.supabase.com:6543/postgres?ssl=require
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/pitchiq"

    # Redis — Upstash (TLS) or local
    # Upstash format: rediss://default:[pass]@[endpoint].upstash.io:6379
    # Local format:   redis://localhost:6379
    REDIS_URL: str = "redis://localhost:6379"

    # API Keys
    GROQ_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    JINA_API_KEY: str = ""
    TAVILY_API_KEY: str = ""

    # JWT (for future auth)
    SECRET_KEY: str = "changeme"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # CORS
    CORS_ORIGINS: List[str] = ["http://localhost:3000", "http://localhost:8000"]

    # Embedding model (Jina AI API)
    EMBEDDING_MODEL: str = "jina-embeddings-v3"

    # Cache settings
    CACHE_ENABLED: bool = True
    CACHE_SIMILARITY_THRESHOLD: float = 0.92

    # Model identifiers
    # Both tiers use Groq for now. To enable Claude for premium,
    # change PREMIUM_MODEL to "claude-sonnet-4-20250514".
    GROQ_CHEAP_MODEL: str = "llama-3.1-8b-instant"
    CLAUDE_QUALITY_MODEL: str = "claude-sonnet-4-20250514"
    PREMIUM_MODEL: str = "llama-3.1-8b-instant"

    # Iterative refinement
    # FREE tier: always 1 iteration (single-pass, no critic loop)
    # PREMIUM tier: up to MAX_CRITIC_ITERATIONS, exits early if score >= threshold
    FREE_CRITIC_ITERATIONS: int = 1
    MAX_CRITIC_ITERATIONS: int = 3       # was 5 — 3 is enough, saves ~40s
    PREMIUM_QUALITY_THRESHOLD: float = 7.5  # was 8.5 — realistic for Groq self-eval

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"  # allow deployment vars (DOMAIN, CERTBOT_EMAIL) in .env


settings = Settings()
