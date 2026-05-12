"""Add agent_tasks table.

Revision ID: 0002
Revises: 0001
Create Date: 2025-05-11
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_tasks (
            id                TEXT PRIMARY KEY,
            user_id           TEXT,
            original_task     TEXT NOT NULL,
            user_tier         TEXT NOT NULL,
            status            TEXT NOT NULL DEFAULT 'pending',
            plan              JSONB,
            agent_outputs     JSONB DEFAULT '{}',
            final_output      JSONB,
            total_cost_usd    FLOAT DEFAULT 0.0,
            total_tokens      INTEGER DEFAULT 0,
            error_message     TEXT,
            created_at        TIMESTAMPTZ DEFAULT NOW(),
            updated_at        TIMESTAMPTZ DEFAULT NOW()
        );
        """
    )

    # Index for status queries
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS agent_tasks_status_idx
        ON agent_tasks (status, created_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_tasks;")
