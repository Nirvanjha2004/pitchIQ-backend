"""
Shared task state stored in PostgreSQL.

Manages agent_tasks table for tracking multi-agent execution.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import Column, String, Text, Float, Integer, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import Base
from app.utils.logger import get_logger

logger = get_logger("state")


class AgentTask(Base):
    """Agent task state stored in database."""
    __tablename__ = "agent_tasks"

    id = Column(String, primary_key=True)
    user_id = Column(String, nullable=True)  # Hardcoded for now, auth later
    original_task = Column(Text, nullable=False)
    user_tier = Column(String, nullable=False)  # free|premium
    status = Column(String, default="pending")  # pending|running|completed|failed
    plan = Column(JSONB, nullable=True)  # Planner output
    agent_outputs = Column(JSONB, default={})  # Each agent's output keyed by name
    final_output = Column(JSONB, nullable=True)  # Compiled result
    total_cost_usd = Column(Float, default=0.0)
    total_tokens = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "task_id": self.id,
            "user_id": self.user_id,
            "original_task": self.original_task,
            "user_tier": self.user_tier,
            "status": self.status,
            "plan": self.plan,
            "agent_outputs": self.agent_outputs,
            "final_output": self.final_output,
            "total_cost_usd": self.total_cost_usd,
            "total_tokens": self.total_tokens,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class StateManager:
    """Manages task state in database."""

    @staticmethod
    async def create_task(
        task: str,
        user_tier: str,
        db: AsyncSession,
        user_id: Optional[str] = None,
    ) -> str:
        """Create new task in DB, return task_id."""
        task_id = str(uuid.uuid4())
        agent_task = AgentTask(
            id=task_id,
            user_id=user_id or "default_user",
            original_task=task,
            user_tier=user_tier,
            status="pending",
            agent_outputs={},
        )
        db.add(agent_task)
        await db.commit()
        logger.info("Created task %s: %s", task_id, task[:60])
        return task_id

    @staticmethod
    async def update_status(
        task_id: str,
        status: str,
        db: AsyncSession,
        error_message: Optional[str] = None,
    ) -> None:
        """Update task status."""
        from sqlalchemy import text as sa_text
        params: Dict[str, Any] = {"status": status, "id": task_id}
        if error_message:
            await db.execute(
                sa_text("UPDATE agent_tasks SET status=:status, error_message=:err, updated_at=NOW() WHERE id=:id"),
                {**params, "err": error_message},
            )
        else:
            await db.execute(
                sa_text("UPDATE agent_tasks SET status=:status, updated_at=NOW() WHERE id=:id"),
                params,
            )
        await db.commit()
        logger.info("Task %s status: %s", task_id, status)

    @staticmethod
    async def save_plan(
        task_id: str,
        plan: Dict[str, Any],
        db: AsyncSession,
    ) -> None:
        """Save planner output."""
        import json
        from sqlalchemy import text as sa_text
        await db.execute(
            sa_text("UPDATE agent_tasks SET plan=:plan::jsonb, updated_at=NOW() WHERE id=:id"),
            {"plan": json.dumps(plan), "id": task_id},
        )
        await db.commit()
        logger.info("Saved plan for task %s", task_id)

    @staticmethod
    async def save_agent_output(
        task_id: str,
        agent_name: str,
        output: Dict[str, Any],
        db: AsyncSession,
    ) -> None:
        """Save individual agent output via raw asyncpg connection."""
        import json

        output_json = json.dumps(output)
        try:
            # Get the raw asyncpg connection — bypasses SQLAlchemy param handling
            raw_conn = await db.connection()
            await raw_conn.exec_driver_sql(
                "UPDATE agent_tasks "
                "SET agent_outputs = COALESCE(agent_outputs, '{}'::jsonb) "
                "    || jsonb_build_object($1::text, $2::jsonb), "
                "    updated_at = NOW() "
                "WHERE id = $3",
                (agent_name, output_json, task_id),
            )
            await db.commit()
            logger.info("Saved output for agent %s in task %s", agent_name, task_id)
        except Exception as exc:
            await db.rollback()
            logger.error("Failed to save agent output for %s: %s", agent_name, exc)
            raise

    @staticmethod
    async def get_context(task_id: str, db: AsyncSession) -> Dict[str, Any]:
        """Get all agent outputs so far — reads directly from DB, bypasses session cache."""
        from sqlalchemy import text as sa_text
        result = await db.execute(
            sa_text("SELECT agent_outputs FROM agent_tasks WHERE id = :id"),
            {"id": task_id},
        )
        row = result.fetchone()
        if row and row[0]:
            return row[0]
        return {}

    @staticmethod
    async def get_task(task_id: str, db: AsyncSession) -> Optional[AgentTask]:
        """Get full task object."""
        from sqlalchemy import text as sa_text
        result = await db.execute(
            sa_text("SELECT * FROM agent_tasks WHERE id = :id"),
            {"id": task_id},
        )
        row = result.mappings().fetchone()
        if not row:
            return None
        task = AgentTask()
        for key, value in row.items():
            setattr(task, key, value)
        return task

    @staticmethod
    async def complete_task(
        task_id: str,
        final_output: Dict[str, Any],
        total_cost_usd: float,
        total_tokens: int,
        db: AsyncSession,
    ) -> None:
        """Mark task as completed with final output."""
        import json
        from sqlalchemy import text as sa_text
        await db.execute(
            sa_text(
                "UPDATE agent_tasks SET status='completed', final_output=:output::jsonb, "
                "total_cost_usd=:cost, total_tokens=:tokens, updated_at=NOW() WHERE id=:id"
            ),
            {
                "output": json.dumps(final_output),
                "cost": total_cost_usd,
                "tokens": total_tokens,
                "id": task_id,
            },
        )
        await db.commit()
        logger.info("Task %s completed: cost=$%.4f, tokens=%d", task_id, total_cost_usd, total_tokens)

    @staticmethod
    async def fail_task(
        task_id: str,
        error_message: str,
        db: AsyncSession,
    ) -> None:
        """Mark task as failed."""
        from sqlalchemy import text as sa_text
        try:
            await db.rollback()
            await db.execute(
                sa_text("UPDATE agent_tasks SET status='failed', error_message=:err, updated_at=NOW() WHERE id=:id"),
                {"err": error_message, "id": task_id},
            )
            await db.commit()
            logger.error("Task %s failed: %s", task_id, error_message)
        except Exception as exc:
            logger.error("Could not mark task %s as failed: %s", task_id, exc)
