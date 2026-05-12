"""
Planner Agent — receives raw user task, produces structured execution plan.

Always uses PREMIUM tier (planning needs best model regardless of user tier).
Output is strict JSON defining which agents to run and their instructions.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from app.agents.base import BaseAgent
from app.utils.logger import get_logger

logger = get_logger("planner")

_PLANNER_SYSTEM_PROMPT = """You are the Planner for PitchIQ, an AI-powered cold outreach system.

Your job: analyze the user's task and produce a strict JSON execution plan.

Available agents (you choose which ones are needed):
- researcher: Searches the web to find companies, news, funding info
- enricher: Finds decision maker names, roles, LinkedIn for each company
- writer: Generates personalized cold outreach emails

Rules:
- Never include yourself (planner) in agents_required
- Never include "critic" — it runs automatically after all agents
- agent_instructions must describe WHAT to find, not HOW (never mention Tavily, web search, or tool names)
- Instructions must be plain search intent: e.g. "Find 4 US startups with recent Series A funding"
- DEFAULT: always include all three agents [researcher, enricher, writer]
- PitchIQ's purpose is cold outreach — always generate emails unless user explicitly says "no emails", "just research", "only list", or "no outreach"
- Only skip writer if user explicitly opts out of emails
- Only skip enricher if there are no companies to look up

Return ONLY this JSON, no preamble, no markdown:
{
  "task_summary": "one sentence describing what will be done",
  "agents_required": ["researcher", "enricher", "writer"],
  "agent_instructions": {
    "researcher": "plain description of what companies/info to find",
    "enricher": "specific enrichment instructions",
    "writer": "specific writing instructions with tone/style guidance"
  },
  "expected_output": "description of final deliverable"
}"""


class PlannerAgent(BaseAgent):
    """Analyzes user task and produces structured agent execution plan."""

    def __init__(self, tier: str = "free"):
        # Planner uses "agent" tier — direct LLM call, no critic, no cache
        super().__init__(name="planner", role="orchestration", tier="agent")

    async def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generate execution plan for the given task.

        Returns:
            Dict with task_summary, agents_required, agent_instructions,
            expected_output.
        """
        logger.info("[planner] Planning task: %s", task[:80])

        messages = [
            {"role": "system", "content": _PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": f"Task: {task}"},
        ]

        gw = await self._call_gateway(
            prompt=task,
            messages=messages,
            tier_override="agent",   # direct call — no critic, no cache
            skip_cache=True,         # planner always needs fresh plan
        )

        plan = self._parse_plan(gw.response, task)
        logger.info(
            "[planner] Plan: agents=%s summary=%s",
            plan["agents_required"],
            plan["task_summary"][:60],
        )
        return plan

    def _parse_plan(self, raw: str, original_task: str) -> Dict[str, Any]:
        """Parse planner JSON output. Falls back to a safe default on failure."""
        # Strip markdown code fences if present
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if len(lines) > 2 else text

        try:
            plan = json.loads(text)
            # Validate required keys
            required = {"task_summary", "agents_required", "agent_instructions", "expected_output"}
            if not required.issubset(plan.keys()):
                raise ValueError(f"Missing keys: {required - plan.keys()}")

            # Ensure planner/critic not in agents_required
            plan["agents_required"] = [
                a for a in plan["agents_required"]
                if a not in ("planner", "critic")
            ]
            return plan

        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("[planner] Failed to parse plan JSON (%s), using fallback", exc)
            return {
                "task_summary": original_task[:100],
                "agents_required": ["researcher", "enricher", "writer"],
                "agent_instructions": {
                    "researcher": f"Research companies relevant to: {original_task}",
                    "enricher": "Find founder/CEO names and contact info for each company",
                    "writer": "Write personalized cold outreach emails for each company",
                },
                "expected_output": "Personalized cold emails with contact information",
            }
