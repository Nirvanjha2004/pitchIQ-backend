"""
Task API endpoints + Dashboard endpoints + SSE streaming.

POST /api/v1/task                           — create task, start pipeline, return task_id immediately
GET  /api/v1/task/{task_id}/stream          — SSE stream of pipeline events
GET  /api/v1/task/{task_id}                 — get task status + result
GET  /api/v1/dashboard/stats
GET  /api/v1/dashboard/tasks
GET  /api/v1/dashboard/tasks/{id}/breakdown
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db, AsyncSessionLocal
from app.orchestrator.pipeline import AgentPipeline
from app.orchestrator.state import StateManager
from app.utils.logger import get_logger

logger = get_logger("api.tasks")

router = APIRouter()
state_manager = StateManager()


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class TaskRequest(BaseModel):
    task: str
    user_tier: Literal["free", "premium"] = "free"
    user_id: Optional[str] = None


class TaskStartResponse(BaseModel):
    task_id: str
    status: str
    message: str
    final_output: Optional[Dict[str, Any]] = None


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    plan: Optional[Dict[str, Any]] = None
    final_output: Optional[Dict[str, Any]] = None
    total_cost_usd: Optional[float] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None


class DashboardStats(BaseModel):
    total_tasks: int
    completed_tasks: int
    failed_tasks: int
    total_cost_usd: float
    total_tokens: int
    average_critic_score: float
    average_execution_time_ms: float


class TaskSummary(BaseModel):
    task_id: str
    original_task: str
    status: str
    user_tier: str
    total_cost_usd: float
    critic_score: float
    agents_used: List[str]
    execution_time_ms: int
    created_at: str


class DashboardTasksResponse(BaseModel):
    tasks: List[TaskSummary]


class AgentDetail(BaseModel):
    model_config = {"protected_namespaces": ()}

    agent_name: str
    model_used: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    latency_ms: int
    success: bool


class TaskBreakdown(BaseModel):
    task_id: str
    original_task: str
    agents: List[AgentDetail]
    total_cost_usd: float
    total_tokens: int
    execution_time_ms: int
    critic_score: float


# ── Task endpoints ────────────────────────────────────────────────────────────

@router.post("/task", response_model=TaskStartResponse, summary="Start agent task")
async def run_task(
    request: TaskRequest,
    db: AsyncSession = Depends(get_db),
) -> TaskStartResponse:
    """
    Start a multi-agent task.

    Creates task_id immediately and returns it, then runs the pipeline
    in a background asyncio task. Client should connect to SSE stream
    right after receiving task_id to get live events.
    """
    if not request.task.strip():
        raise HTTPException(status_code=422, detail="Task cannot be empty")

    logger.info("Starting task: %s (tier=%s)", request.task[:80], request.user_tier)

    # Pre-create task so client gets task_id immediately
    task_id = await state_manager.create_task(
        request.task, request.user_tier, db, request.user_id
    )

    # Run pipeline in background — SSE stream will carry live events
    asyncio.create_task(
        _run_pipeline_background(
            task=request.task,
            user_tier=request.user_tier,
            user_id=request.user_id,
            task_id=task_id,
        )
    )

    return TaskStartResponse(
        task_id=task_id,
        status="pending",
        message="Task started. Connect to SSE stream for live updates.",
        final_output=None,
    )


async def _run_pipeline_background(
    task: str,
    user_tier: str,
    user_id: Optional[str],
    task_id: str,
) -> None:
    """Run the pipeline in a background task with its own DB session."""
    async with AsyncSessionLocal() as db:
        try:
            pipeline = AgentPipeline()
            await pipeline.run(
                task=task,
                user_tier=user_tier,
                db=db,
                user_id=user_id,
                task_id=task_id,
            )
        except Exception as exc:
            logger.error("Background pipeline failed for task %s: %s", task_id, exc)


@router.get("/task/{task_id}/stream", summary="SSE stream of pipeline events")
async def stream_task_events(task_id: str) -> StreamingResponse:
    """
    Server-Sent Events stream for a running task.

    Polls Redis list task_events:{task_id} every 500ms and yields new events.
    Closes when it sees the "DONE" sentinel.
    """
    return StreamingResponse(
        _sse_generator(task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


async def _sse_generator(task_id: str) -> AsyncGenerator[str, None]:
    """Poll Redis and yield SSE events until DONE sentinel."""
    import redis as redis_sync
    from app.config import settings

    url = settings.REDIS_URL
    try:
        if url.startswith("rediss://"):
            r = redis_sync.from_url(url, decode_responses=False, ssl_cert_reqs=None)
        else:
            r = redis_sync.from_url(url, decode_responses=False)
    except Exception as exc:
        yield f"data: {json.dumps({'type': 'task_failed', 'message': f'Redis unavailable: {exc}'})}\n\n"
        yield "data: [DONE]\n\n"
        return

    key = f"task_events:{task_id}"
    cursor = 0
    max_polls = 1200  # 10 minutes max (1200 × 0.5s)

    for _ in range(max_polls):
        try:
            events = r.lrange(key, cursor, -1)
        except Exception:
            await asyncio.sleep(0.5)
            continue

        for raw in events:
            cursor += 1
            if raw == b"DONE":
                yield "data: [DONE]\n\n"
                return
            try:
                yield f"data: {raw.decode('utf-8')}\n\n"
            except Exception:
                pass

        await asyncio.sleep(0.5)

    # Timeout
    yield f"data: {json.dumps({'type': 'task_failed', 'message': 'Stream timeout'})}\n\n"
    yield "data: [DONE]\n\n"


@router.get("/task/{task_id}", response_model=TaskStatusResponse, summary="Get task status")
async def get_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
) -> TaskStatusResponse:
    """Get task status and result by task_id."""
    task = await state_manager.get_task(task_id, db)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    return TaskStatusResponse(
        task_id=task.id,
        status=task.status,
        plan=task.plan,
        final_output=task.final_output,
        total_cost_usd=task.total_cost_usd,
        error_message=task.error_message,
        created_at=task.created_at.isoformat() if task.created_at else None,
    )


# ── Dashboard endpoints ───────────────────────────────────────────────────────

@router.get("/dashboard/stats", response_model=DashboardStats, summary="Dashboard aggregate stats")
async def dashboard_stats(db: AsyncSession = Depends(get_db)) -> DashboardStats:
    """Aggregate stats across all agent_tasks."""
    result = await db.execute(
        text(
            """
            SELECT
                COUNT(*)                                                AS total_tasks,
                COUNT(*) FILTER (WHERE status = 'completed')           AS completed_tasks,
                COUNT(*) FILTER (WHERE status = 'failed')              AS failed_tasks,
                COALESCE(SUM(total_cost_usd), 0)                       AS total_cost_usd,
                COALESCE(SUM(total_tokens), 0)                         AS total_tokens,
                COALESCE(
                    AVG(
                        CASE WHEN final_output->>'critic_score' IS NOT NULL
                             THEN (final_output->>'critic_score')::float
                        END
                    ), 0
                )                                                       AS average_critic_score,
                COALESCE(
                    AVG(
                        CASE WHEN final_output->>'execution_time_ms' IS NOT NULL
                             THEN (final_output->>'execution_time_ms')::float
                        END
                    ), 0
                )                                                       AS average_execution_time_ms
            FROM agent_tasks
            """
        )
    )
    row = result.fetchone()
    return DashboardStats(
        total_tasks=int(row[0] or 0),
        completed_tasks=int(row[1] or 0),
        failed_tasks=int(row[2] or 0),
        total_cost_usd=float(row[3] or 0),
        total_tokens=int(row[4] or 0),
        average_critic_score=float(row[5] or 0),
        average_execution_time_ms=float(row[6] or 0),
    )


@router.get("/dashboard/tasks", response_model=DashboardTasksResponse, summary="Last 20 tasks")
async def dashboard_tasks(db: AsyncSession = Depends(get_db)) -> DashboardTasksResponse:
    """Return last 20 tasks ordered by created_at DESC."""
    result = await db.execute(
        text(
            """
            SELECT
                id,
                original_task,
                status,
                user_tier,
                COALESCE(total_cost_usd, 0)                                     AS total_cost_usd,
                COALESCE((final_output->>'critic_score')::float, 0)             AS critic_score,
                COALESCE(final_output->'agents_used', '[]'::jsonb)              AS agents_used,
                COALESCE((final_output->>'execution_time_ms')::int, 0)          AS execution_time_ms,
                created_at
            FROM agent_tasks
            ORDER BY created_at DESC
            LIMIT 20
            """
        )
    )
    rows = result.fetchall()

    tasks = []
    for row in rows:
        agents_used = row[6]
        if isinstance(agents_used, str):
            import json
            try:
                agents_used = json.loads(agents_used)
            except Exception:
                agents_used = []

        tasks.append(TaskSummary(
            task_id=str(row[0]),
            original_task=str(row[1]),
            status=str(row[2]),
            user_tier=str(row[3]),
            total_cost_usd=float(row[4] or 0),
            critic_score=float(row[5] or 0),
            agents_used=agents_used if isinstance(agents_used, list) else [],
            execution_time_ms=int(row[7] or 0),
            created_at=row[8].isoformat() if row[8] else "",
        ))

    return DashboardTasksResponse(tasks=tasks)


@router.get(
    "/dashboard/tasks/{task_id}/breakdown",
    response_model=TaskBreakdown,
    summary="Per-agent token/cost breakdown for a task",
)
async def task_breakdown(
    task_id: str,
    db: AsyncSession = Depends(get_db),
) -> TaskBreakdown:
    """
    Return per-agent breakdown by joining agent_tasks with token_usage.
    Falls back to agent_outputs JSONB when token_usage has no rows for this task.
    """
    # Get task metadata
    task_result = await db.execute(
        text(
            """
            SELECT
                id,
                original_task,
                COALESCE(total_cost_usd, 0)                                 AS total_cost_usd,
                COALESCE(total_tokens, 0)                                    AS total_tokens,
                COALESCE((final_output->>'execution_time_ms')::int, 0)      AS execution_time_ms,
                COALESCE((final_output->>'critic_score')::float, 0)         AS critic_score,
                final_output->'agents_used'                                 AS agents_used,
                agent_outputs                                                AS agent_outputs
            FROM agent_tasks
            WHERE id = :task_id
            """
        ),
        {"task_id": task_id},
    )
    task_row = task_result.fetchone()
    if not task_row:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    # Try token_usage table first
    usage_result = await db.execute(
        text(
            """
            SELECT
                model_used,
                SUM(input_tokens)           AS input_tokens,
                SUM(output_tokens)          AS output_tokens,
                SUM(estimated_cost_usd)     AS cost
            FROM token_usage
            WHERE task_id = :task_id
            GROUP BY model_used
            ORDER BY cost DESC
            """
        ),
        {"task_id": task_id},
    )
    usage_rows = usage_result.fetchall()

    # Build agent details from agents_used list + token_usage
    import json

    agents_used_raw = task_row[6]
    if isinstance(agents_used_raw, str):
        try:
            agents_used = json.loads(agents_used_raw)
        except Exception:
            agents_used = []
    else:
        agents_used = agents_used_raw or []

    # Build a model map from token_usage
    model_map: Dict[str, Dict] = {}
    for row in usage_rows:
        model_map[str(row[0])] = {
            "input_tokens": int(row[1] or 0),
            "output_tokens": int(row[2] or 0),
            "cost": float(row[3] or 0),
        }

    # Map agents to models (best-effort from agent_outputs)
    agent_outputs_raw = task_row[7] or {}
    if isinstance(agent_outputs_raw, str):
        try:
            agent_outputs = json.loads(agent_outputs_raw)
        except Exception:
            agent_outputs = {}
    else:
        agent_outputs = agent_outputs_raw

    # Default model assignments
    _default_models = {
        "planner": "llama-3.1-8b-instant",
        "researcher": "llama-3.1-8b-instant",
        "enricher": "llama-3.1-8b-instant",
        "writer": "llama-3.1-8b-instant",
        "critic": "llama-3.1-8b-instant",
    }

    agents: List[AgentDetail] = []
    for agent_name in (agents_used if agents_used else list(_default_models.keys())):
        model = _default_models.get(str(agent_name).lower(), "llama-3.1-8b-instant")
        usage = model_map.get(model, {})
        agents.append(AgentDetail(
            agent_name=str(agent_name),
            model_used=model,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            estimated_cost_usd=usage.get("cost", 0.0),
            latency_ms=0,  # Not tracked per-agent yet
            success=str(agent_name).lower() in agent_outputs,
        ))

    return TaskBreakdown(
        task_id=str(task_row[0]),
        original_task=str(task_row[1]),
        agents=agents,
        total_cost_usd=float(task_row[2] or 0),
        total_tokens=int(task_row[3] or 0),
        execution_time_ms=int(task_row[4] or 0),
        critic_score=float(task_row[5] or 0),
    )
