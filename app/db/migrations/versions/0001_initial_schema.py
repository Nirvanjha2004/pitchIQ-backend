"""Initial schema — prompt_cache and token_usage tables with pgvector.

Revision ID: 0001
Revises:
Create Date: 2025-05-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enable pgvector extension
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")

    # ── prompt_cache ──────────────────────────────────────────────────────────
    # jina-embeddings-v3 produces 1024-dim vectors
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS prompt_cache (
            id          TEXT PRIMARY KEY,
            prompt_text TEXT NOT NULL,
            embedding   vector(1024) NOT NULL,
            response_text TEXT NOT NULL,
            model_used  TEXT NOT NULL,
            quality_score FLOAT DEFAULT 0.0,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        );
        """
    )

    # IVFFlat index for fast approximate nearest-neighbour search
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS prompt_cache_embedding_idx
        ON prompt_cache
        USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100);
        """
    )

    # ── token_usage ───────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS token_usage (
            id                  TEXT PRIMARY KEY,
            user_id             TEXT,
            model_used          TEXT NOT NULL,
            input_tokens        INTEGER DEFAULT 0,
            output_tokens       INTEGER DEFAULT 0,
            estimated_cost_usd  FLOAT DEFAULT 0.0,
            task_id             TEXT,
            created_at          TIMESTAMPTZ DEFAULT NOW()
        );
        """
    )

    # ── users (stub — auth comes later) ──────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            TEXT PRIMARY KEY,
            email         TEXT UNIQUE,
            password_hash TEXT,
            created_at    TIMESTAMPTZ DEFAULT NOW()
        );
        """
    )

    # ── tasks ─────────────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id           TEXT PRIMARY KEY,
            user_id      TEXT REFERENCES users(id),
            status       TEXT DEFAULT 'pending',
            input_data   JSONB,
            output_data  JSONB,
            created_at   TIMESTAMPTZ DEFAULT NOW(),
            completed_at TIMESTAMPTZ
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tasks;")
    op.execute("DROP TABLE IF EXISTS token_usage;")
    op.execute("DROP TABLE IF EXISTS prompt_cache;")
    op.execute("DROP TABLE IF EXISTS users;")
    op.execute("DROP EXTENSION IF EXISTS vector;")
