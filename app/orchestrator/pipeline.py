"""
Tiered inference pipeline — the core of the LLM gateway.

FREE tier:
  1. Check semantic cache → return on hit
  2. Call Groq llama-3.1-8b-instant
  3. Run critic once (Groq)
  4. Return response + quality_score regardless of score
  5. Store in cache

PREMIUM tier:
  1. Check semantic cache → return on hit
  2. Call Claude Sonnet
  3. Run critic (Groq)
  4. If score >= 8.5 → return
  5. If score < 8.5 → append feedback, rewrite with Claude
  6. Repeat until score >= 8.5 OR max 5 iterations
  7. Return best response with iteration count
  8. Store in cache
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.critic import run_critic
from app.config import settings
from app.gateway import cache as semantic_cache
from app.gateway.proxy import call_llm
from app.gateway.router import route
from app.services.token_tracker import record_usage
from app.utils.logger import get_logger

# Fallback model when primary call fails
_FALLBACK_MODEL = settings.PREMIUM_MODEL

logger = get_logger("pipeline")


@dataclass
class PipelineResult:
    response: str
    model_used: str
    quality_score: float
    iterations: int
    cached: bool
    estimated_cost_usd: float
    note: Optional[str] = None


async def run_inference(
    prompt: str,
    messages: list[dict],
    user_tier: str,
    db: AsyncSession,
    user_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> PipelineResult:
    """
    Execute the full tiered inference pipeline.

    Args:
        prompt:     The user's prompt text (used for cache key and critic).
        messages:   Full conversation history in OpenAI format.
        user_tier:  "free" or "premium".
        db:         Async DB session for cache + token tracking.
        user_id:    Optional user identifier for token tracking.
        task_id:    Optional task identifier for token tracking.

    Returns:
        PipelineResult with all metadata.
    """
    decision = route(user_tier)
    model = decision["model"]
    strategy = decision["strategy"]

    # ── agent_direct: skip cache + critic entirely ───────────────────────────
    # Used by planner, researcher, enricher, critic agents — not writer.
    # Saves ~15s per call (no critic round trip, no Jina embed round trips).
    if strategy == "agent_direct":
        try:
            llm_response = await call_llm(model=model, messages=messages)
        except Exception as exc:
            logger.warning("agent_direct: %s failed (%s), falling back to %s", model, exc, _FALLBACK_MODEL)
            llm_response = await call_llm(model=_FALLBACK_MODEL, messages=messages)
        await record_usage(
            model_used=llm_response.model,
            input_tokens=llm_response.input_tokens,
            output_tokens=llm_response.output_tokens,
            db=db, user_id=user_id, task_id=task_id,
        )
        return PipelineResult(
            response=llm_response.text,
            model_used=llm_response.model,
            quality_score=0.0,
            iterations=1,
            cached=False,
            estimated_cost_usd=0.0,  # tracked in token_usage
            note=None,
        )

    # ── 1. Semantic cache check (only for free/premium user-facing calls) ────
    cached_result = await semantic_cache.cache_get(prompt, db)
    if cached_result:
        return PipelineResult(
            response=cached_result.response_text,
            model_used=cached_result.model_used,
            quality_score=cached_result.quality_score,
            iterations=0,
            cached=True,
            estimated_cost_usd=0.0,
            note=_tier_note(user_tier),
        )

    # ── 2. LLM call + critic loop ────────────────────────────────────────────
    if strategy == "single_pass":
        # FREE tier: exactly 1 iteration, no critic loop
        result = await _run_free_pipeline(prompt, messages, model, db, user_id, task_id)
    else:
        # PREMIUM tier: up to MAX_CRITIC_ITERATIONS (5), exits early on score >= 8.5
        result = await _run_premium_pipeline(prompt, messages, model, db, user_id, task_id)

    # ── 3. Store in cache ────────────────────────────────────────────────────
    await semantic_cache.cache_set(
        prompt=prompt,
        response_text=result.response,
        model_used=result.model_used,
        quality_score=result.quality_score,
        db=db,
    )

    return result


# ── Free tier ────────────────────────────────────────────────────────────────

async def _run_free_pipeline(
    prompt: str,
    messages: list[dict],
    model: str,
    db: AsyncSession,
    user_id: Optional[str],
    task_id: Optional[str],
) -> PipelineResult:
    total_cost = 0.0

    # LLM call — fallback to premium model if primary fails
    try:
        llm_response = await call_llm(model=model, messages=messages)
    except Exception as exc:
        logger.warning("Primary model %s failed (%s), falling back to %s", model, exc, _FALLBACK_MODEL)
        llm_response = await call_llm(model=_FALLBACK_MODEL, messages=messages)

    cost = await record_usage(
        model_used=llm_response.model,
        input_tokens=llm_response.input_tokens,
        output_tokens=llm_response.output_tokens,
        db=db,
        user_id=user_id,
        task_id=task_id,
    )
    total_cost += cost

    # Critic — single pass, result shown to user but does NOT gate the response
    try:
        critic_result = await run_critic(prompt, llm_response.text)
        quality_score = critic_result.score
        critic_input_tokens = critic_result.input_tokens
        critic_output_tokens = critic_result.output_tokens
    except Exception as exc:
        logger.warning("Critic failed (non-fatal), skipping: %s", exc)
        quality_score = 0.0
        critic_input_tokens = 0
        critic_output_tokens = 0

    critic_cost = await record_usage(
        model_used=settings.GROQ_CHEAP_MODEL,
        input_tokens=critic_input_tokens,
        output_tokens=critic_output_tokens,
        db=db,
        user_id=user_id,
        task_id=task_id,
    )
    total_cost += critic_cost

    return PipelineResult(
        response=llm_response.text,
        model_used=llm_response.model,
        quality_score=quality_score,
        iterations=1,
        cached=False,
        estimated_cost_usd=total_cost,
        note="Upgrade to Premium for iterative refinement",
    )


# ── Premium tier ─────────────────────────────────────────────────────────────

async def _run_premium_pipeline(
    prompt: str,
    messages: list[dict],
    model: str,
    db: AsyncSession,
    user_id: Optional[str],
    task_id: Optional[str],
) -> PipelineResult:
    total_cost = 0.0
    current_messages = list(messages)

    best_response: str = ""
    best_score: float = 0.0
    best_model: str = model
    iteration = 0
    note: Optional[str] = None

    for iteration in range(1, settings.MAX_CRITIC_ITERATIONS + 1):  # max 5 for premium
        # LLM call — fallback if primary fails
        try:
            llm_response = await call_llm(model=model, messages=current_messages)
        except Exception as exc:
            logger.warning("Primary model %s failed (%s), falling back to %s", model, exc, _FALLBACK_MODEL)
            llm_response = await call_llm(model=_FALLBACK_MODEL, messages=current_messages)

        cost = await record_usage(
            model_used=llm_response.model,
            input_tokens=llm_response.input_tokens,
            output_tokens=llm_response.output_tokens,
            db=db,
            user_id=user_id,
            task_id=task_id,
        )
        total_cost += cost

        # Critic
        try:
            critic_result = await run_critic(prompt, llm_response.text)
            current_score = critic_result.score
            critic_input_tokens = critic_result.input_tokens
            critic_output_tokens = critic_result.output_tokens
        except Exception as exc:
            logger.warning("Critic failed (non-fatal), using score=0.0: %s", exc)
            current_score = 0.0
            critic_input_tokens = 0
            critic_output_tokens = 0

        critic_cost = await record_usage(
            model_used=settings.GROQ_CHEAP_MODEL,
            input_tokens=critic_input_tokens,
            output_tokens=critic_output_tokens,
            db=db,
            user_id=user_id,
            task_id=task_id,
        )
        total_cost += critic_cost

        logger.info(
            "Premium iteration %d/%d — score=%.1f",
            iteration,
            settings.MAX_CRITIC_ITERATIONS,
            current_score,
        )

        if current_score > best_score:
            best_score = current_score
            best_response = llm_response.text
            best_model = llm_response.model

        if current_score >= settings.PREMIUM_QUALITY_THRESHOLD:
            break

        if current_score > 0:
            feedback = getattr(critic_result, "feedback", "Improve quality")
            current_messages = current_messages + [
                {"role": "assistant", "content": llm_response.text},
                {
                    "role": "user",
                    "content": (
                        f"The previous response scored {current_score:.1f}/10. "
                        f"Feedback: {feedback}. "
                        "Please rewrite the response addressing this feedback."
                    ),
                },
            ]
        else:
            logger.warning("Critic unavailable, exiting iteration loop early")
            break
    else:
        note = "Max iterations reached"
        logger.info("Premium pipeline hit max iterations. Best score: %.1f", best_score)

    return PipelineResult(
        response=best_response,
        model_used=best_model,
        quality_score=best_score,
        iterations=iteration,
        cached=False,
        estimated_cost_usd=total_cost,
        note=note,
    )


def _tier_note(user_tier: str) -> Optional[str]:
    if user_tier == "free":
        return "Upgrade to Premium for iterative refinement"
    return None


# ═════════════════════════════════════════════════════════════════════════════
# Agent Orchestration Pipeline (separate from gateway inference)
# ═════════════════════════════════════════════════════════════════════════════

from typing import Any, Dict, List


class AgentPipeline:
    """
    Executes the multi-agent orchestration plan.

    Flow:
    1. Create task in DB
    2. Call PlannerAgent → get plan
    3. Save plan to DB, update status: running
    4. Loop through plan.agents_required in order
    5. Always run CriticAgent last
    6. Compile final_output
    7. Update status: completed
    """

    def __init__(self):
        from app.agents.planner import PlannerAgent
        from app.agents.researcher import ResearcherAgent
        from app.agents.enricher import EnricherAgent
        from app.agents.writer import WriterAgent
        from app.agents.critic import CriticAgent
        from app.orchestrator.state import StateManager

        self.state = StateManager()
        self._agent_registry = {
            "researcher": ResearcherAgent,
            "enricher": EnricherAgent,
            "writer": WriterAgent,
        }
        self._planner = PlannerAgent()
        self._critic = CriticAgent()

    async def run(
        self,
        task: str,
        user_tier: str,
        db: AsyncSession,
        user_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Execute full agent pipeline for a task.

        Args:
            task_id: If provided, use this ID (pre-created). Otherwise create new.
        Returns final_output dict.
        """
        from app.services.event_emitter import (
            emit_task_started, emit_plan_ready, emit_agent_started,
            emit_agent_log, emit_agent_completed, emit_task_completed,
            emit_task_failed,
        )

        start_time = time.monotonic()
        from datetime import datetime, timezone
        task_start_dt = datetime.now(timezone.utc)

        # 1. Create task in DB (or use pre-created id)
        if task_id is None:
            task_id = await self.state.create_task(task, user_tier, db, user_id)
        logger.info("AgentPipeline starting task %s", task_id)

        emit_task_started(task_id, "Task received. Analyzing...")

        # Track agent instances so we can read their accumulated costs
        agent_instances: Dict[str, Any] = {}

        try:
            # 2. Plan
            logger.info("[pipeline] Running planner...")
            emit_agent_started(task_id, "planner", "Planner analyzing task...")
            t0 = time.monotonic()
            plan = await self._planner.execute(task, {})
            planner_ms = int((time.monotonic() - t0) * 1000)

            await self.state.save_plan(task_id, plan, db)
            await self.state.update_status(task_id, "running", db)

            agents_required: List[str] = plan.get("agents_required", [])
            agent_instructions: Dict[str, str] = plan.get("agent_instructions", {})

            # Safety net: writer should almost always run — add it if planner missed it
            # (planner sometimes skips writer when task doesn't explicitly say "email")
            if "writer" not in agents_required and "researcher" in agents_required:
                logger.info("[pipeline] Writer missing from plan — adding it (PitchIQ always generates emails)")
                agents_required = agents_required + ["writer"]
                agent_instructions["writer"] = (
                    f"Write personalized cold outreach emails to the founders of the companies found. "
                    f"Reference their specific work and be concise and professional."
                )

            emit_plan_ready(task_id, agents_required)
            emit_agent_completed(
                task_id, "planner",
                f"Plan ready — {len(agents_required)} agents scheduled",
                latency_ms=planner_ms,
                cost_usd=self._planner.total_cost,
            )
            logger.info("[pipeline] Plan: %s", agents_required)

            # 3. Execute each agent in order
            for agent_name in agents_required:
                if agent_name not in self._agent_registry:
                    logger.warning("[pipeline] Unknown agent '%s', skipping", agent_name)
                    continue

                instruction = str(agent_instructions.get(agent_name, task))
                context = await self.state.get_context(task_id, db)
                context["_task_id"] = task_id  # inject for live log emission

                # All agents use "agent" tier → agent_direct strategy (no critic loop,
                # no cache). The orchestrator's CriticAgent handles quality at the end.
                # Previously writer used user_tier="premium" which triggered up to 3
                # gateway-level critic iterations per call — and those could restart if
                # the HTTP retry in _call_gateway fired, causing a fresh iteration set.
                agent_tier = "agent"

                logger.info("[pipeline] Running agent: %s (tier=%s)", agent_name, agent_tier)
                emit_agent_started(task_id, agent_name, f"{agent_name.capitalize()} starting...")

                agent = self._agent_registry[agent_name](tier=agent_tier)
                agent_instances[agent_name] = agent

                t0 = time.monotonic()
                try:
                    output = await agent.execute(instruction, context)
                    agent_ms = int((time.monotonic() - t0) * 1000)
                    await self.state.save_agent_output(task_id, agent_name, output, db)

                    # Emit meaningful log lines from output
                    _emit_agent_logs(task_id, agent_name, output)

                    emit_agent_completed(
                        task_id, agent_name,
                        f"{agent_name.capitalize()} done",
                        latency_ms=agent_ms,
                        cost_usd=agent.total_cost,
                    )
                    logger.info("[pipeline] Agent %s completed (cost=$%.5f)", agent_name, agent.total_cost)
                except Exception as exc:
                    error_msg = f"Agent '{agent_name}' failed: {exc}"
                    logger.error("[pipeline] %s", error_msg)
                    emit_task_failed(task_id, error_msg)
                    await self.state.fail_task(task_id, error_msg, db)
                    raise RuntimeError(error_msg) from exc

            # 4. Always run critic last
            logger.info("[pipeline] Running critic (tier=free)...")
            emit_agent_started(task_id, "critic", "Critic evaluating output quality...")
            t0 = time.monotonic()
            final_context = await self.state.get_context(task_id, db)
            try:
                critic_output = await self._critic.execute(task, final_context)
                critic_ms = int((time.monotonic() - t0) * 1000)
                await self.state.save_agent_output(task_id, "critic", critic_output, db)
                score = critic_output.get("overall_score", 0.0)
                emit_agent_log(task_id, "critic", f"Score: {score:.1f}/10 — {critic_output.get('feedback', '')[:80]}")
                emit_agent_completed(
                    task_id, "critic",
                    f"Critic done — {score:.1f}/10",
                    latency_ms=critic_ms,
                    cost_usd=self._critic.total_cost,
                )
            except Exception as exc:
                logger.warning("[pipeline] Critic failed (non-fatal): %s", exc)
                critic_output = {"overall_score": 0.0, "feedback": "Evaluation unavailable", "emails_reviewed": 0}

            # 5. Compile final output
            execution_time_ms = int((time.monotonic() - start_time) * 1000)
            final_context = await self.state.get_context(task_id, db)

            actual_cost = (
                self._planner.total_cost
                + self._critic.total_cost
                + sum(agent_instances[a].total_cost for a in agents_required if a in agent_instances)
            )
            from sqlalchemy import text as sa_text
            token_result = await db.execute(
                sa_text("SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM token_usage WHERE created_at >= :since"),
                {"since": task_start_dt},
            )
            actual_tokens = int(token_result.scalar() or 0)

            final_output = self._compile_output(
                task=task, plan=plan, context=final_context,
                critic_output=critic_output, agents_used=agents_required,
                execution_time_ms=execution_time_ms,
                total_cost=actual_cost, total_tokens=actual_tokens,
            )

            # 6. Complete task
            await self.state.complete_task(
                task_id=task_id, final_output=final_output,
                total_cost_usd=actual_cost, total_tokens=actual_tokens, db=db,
            )

            emit_task_completed(
                task_id,
                total_cost_usd=actual_cost,
                total_tokens=actual_tokens,
                critic_score=critic_output.get("overall_score", 0.0),
            )

            logger.info("[pipeline] Task %s completed in %dms", task_id, execution_time_ms)
            return {**final_output, "task_id": task_id}

        except Exception as exc:
            emit_task_failed(task_id, str(exc))
            await self.state.fail_task(task_id, str(exc), db)
            raise

    def _compile_output(
        self,
        task: str,
        plan: Dict[str, Any],
        context: Dict[str, Any],
        critic_output: Dict[str, Any],
        agents_used: List[str],
        execution_time_ms: int,
        total_cost: float,
        total_tokens: int,
    ) -> Dict[str, Any]:
        """Compile all agent outputs into final response."""
        writer_output = context.get("writer", {})
        emails = writer_output.get("emails", [])

        researcher_output = context.get("researcher", {})
        companies_researched = len(researcher_output.get("companies", []))

        return {
            "task": task,
            "task_summary": plan.get("task_summary", ""),
            "emails": emails,
            "companies_researched": companies_researched,
            "critic_score": critic_output.get("overall_score", 0.0),
            "critic_feedback": critic_output.get("feedback", ""),
            "total_cost_usd": round(total_cost, 6),
            "total_tokens": total_tokens,
            "agents_used": ["planner"] + agents_used + ["critic"],
            "execution_time_ms": execution_time_ms,
        }


def _emit_agent_logs(task_id: str, agent_name: str, output: Dict[str, Any]) -> None:
    """Emit meaningful log lines from agent output."""
    from app.services.event_emitter import emit_agent_log
    try:
        if agent_name == "researcher":
            companies = output.get("companies", [])
            for c in companies[:5]:
                name = c.get("name", "")
                stage = c.get("funding_stage", "")
                desc = (c.get("description") or "")[:60]
                if name:
                    emit_agent_log(task_id, agent_name, f"Found: {name}{' — ' + stage if stage else ''}{' — ' + desc if desc else ''}")
        elif agent_name == "enricher":
            enriched = output.get("enriched_companies", [])
            for c in enriched[:5]:
                company = c.get("company", "")
                dm = c.get("decision_maker_name")
                role = c.get("decision_maker_role", "")
                if company and dm:
                    emit_agent_log(task_id, agent_name, f"{company} → {dm} ({role})")
                elif company:
                    emit_agent_log(task_id, agent_name, f"{company} → no decision maker found")
        elif agent_name == "writer":
            emails = output.get("emails", [])
            for e in emails[:5]:
                company = e.get("company", "")
                subject = e.get("subject", "")[:50]
                if company:
                    emit_agent_log(task_id, agent_name, f"Email for {company}: \"{subject}\"")
    except Exception:
        pass  # log emission is always non-fatal


# Keep the old Pipeline class for backward compatibility with tasks.py
class Pipeline:
    """Legacy multi-agent pipeline — delegates to AgentPipeline."""

    def __init__(self):
        self._pipeline = AgentPipeline()
        self.execution_log: List[Dict[str, Any]] = []

    async def run(self, task: str, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """Execute full pipeline. Requires db session via config."""
        config = config or {}
        db = config.get("db")
        user_tier = config.get("user_tier", "free")
        user_id = config.get("user_id")

        if db is None:
            raise ValueError("Pipeline.run() requires config['db'] = AsyncSession")

        return await self._pipeline.run(task, user_tier, db, user_id)
