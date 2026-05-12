"""
Critic Agent — two roles in one file:

1. Agent-level critic (CriticAgent class):
   Evaluates the FINAL task output holistically.
   Scores overall output 1-10, gives one actionable improvement.
   Uses FREE tier gateway.
   Runs AFTER all other agents complete, always.

2. Gateway-level critic (run_critic function):
   Used by the inference pipeline to gate/refine LLM responses.
   Returns strict JSON {score, feedback}.
   Always uses Groq cheap model.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from app.agents.base import BaseAgent
from app.utils.logger import get_logger

logger = get_logger("critic")

# ── Gateway-level critic ──────────────────────────────────────────────────────

_INFERENCE_CRITIC_SYSTEM_PROMPT = """You are a strict quality evaluator for AI-generated sales outreach content.

Evaluate the response to the given prompt and return ONLY a JSON object with no preamble, no markdown, no explanation:

{"score": <float between 1.0 and 10.0>, "feedback": "<one sentence describing the single most important improvement>"}

Scoring guide:
- 9-10: Exceptional, publish-ready
- 7-8.9: Good, minor polish needed
- 5-6.9: Acceptable but clearly improvable
- 3-4.9: Weak, significant issues
- 1-2.9: Poor, fundamental problems

Return ONLY the JSON object. Nothing else."""


class CriticResult:
    """Result from the inference critic."""

    def __init__(self, score: float, feedback: str, input_tokens: int, output_tokens: int):
        self.score = score
        self.feedback = feedback
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


async def run_critic(original_prompt: str, current_response: str) -> CriticResult:
    """
    Run the inference critic on a prompt + response pair.

    Used by the gateway pipeline (both FREE and PREMIUM tiers).
    Always uses the cheap Groq model — never Claude.

    Returns CriticResult. On JSON parse failure, defaults to
    score=5.0 and feedback="Could not evaluate".
    """
    from app.config import settings
    from app.gateway.proxy import call_llm

    messages = [
        {"role": "system", "content": _INFERENCE_CRITIC_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"ORIGINAL PROMPT:\n{original_prompt}\n\n"
                f"RESPONSE TO EVALUATE:\n{current_response}"
            ),
        },
    ]

    raw = await call_llm(model=settings.GROQ_CHEAP_MODEL, messages=messages)

    try:
        parsed = json.loads(raw.text.strip())
        score = float(parsed["score"])
        feedback = str(parsed["feedback"])
        score = max(1.0, min(10.0, score))
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        logger.warning(
            "Critic returned invalid JSON (%s). Defaulting score=5.0. Raw: %s",
            exc,
            raw.text[:200],
        )
        score = 5.0
        feedback = "Could not evaluate"

    return CriticResult(
        score=score,
        feedback=feedback,
        input_tokens=raw.input_tokens,
        output_tokens=raw.output_tokens,
    )


# ── Agent-level critic ────────────────────────────────────────────────────────

_AGENT_CRITIC_SYSTEM_PROMPT = """You are a senior B2B sales strategist reviewing AI-generated cold outreach emails.

Evaluate the complete set of emails holistically and return ONLY this JSON, no preamble, no markdown:
{
  "overall_score": <float 1-10>,
  "feedback": "<one actionable improvement that would most increase reply rates>",
  "emails_reviewed": <int>
}

Scoring guide:
- 9-10: Exceptional personalization, compelling CTAs, ready to send
- 7-8.9: Good quality, minor improvements needed
- 5-6.9: Acceptable but generic in places
- 3-4.9: Weak personalization or unclear value proposition
- 1-2.9: Generic templates, unlikely to get replies"""


class CriticAgent(BaseAgent):
    """
    Agent-level critic — evaluates the final task output holistically.
    Runs after all other agents complete.
    """

    def __init__(self, tier: str = "free"):
        super().__init__(name="critic", role="evaluation", tier="agent")

    async def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Evaluate all generated emails holistically.

        Args:
            task:    Original user task.
            context: All agent outputs including writer emails.

        Returns:
            Dict with overall_score, feedback, emails_reviewed.
        """
        logger.info("[critic] Evaluating final output for task: %s", task[:60])

        # Collect all emails from writer output
        writer_output = context.get("writer", {})
        emails: List[Dict[str, Any]] = writer_output.get("emails", [])

        if not emails:
            logger.warning("[critic] No emails to evaluate")
            return {
                "overall_score": 0.0,
                "feedback": "No emails were generated to evaluate.",
                "emails_reviewed": 0,
            }

        # Format emails for review
        emails_text = self._format_emails(emails)

        prompt = f"""Original task: {task}

Generated emails:
{emails_text}

Evaluate these cold outreach emails holistically."""

        messages = [
            {"role": "system", "content": _AGENT_CRITIC_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        gw = await self._call_gateway(prompt=prompt, messages=messages)
        result = self._parse_result(gw.response, len(emails))

        logger.info(
            "[critic] Score: %.1f/10 — %s",
            result["overall_score"],
            result["feedback"][:60],
        )
        return result

    def _format_emails(self, emails: List[Dict[str, Any]]) -> str:
        """Format emails list for critic review."""
        parts = []
        for i, email in enumerate(emails, 1):
            parts.append(
                f"Email {i} — To: {email.get('to', 'Unknown')} at {email.get('company', 'Unknown')}\n"
                f"Subject: {email.get('subject', '')}\n"
                f"{email.get('body', '')}"
            )
        return "\n\n---\n\n".join(parts)

    def _parse_result(self, raw: str, email_count: int) -> Dict[str, Any]:
        """Parse critic JSON with fallback."""
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if len(lines) > 2 else text

        try:
            data = json.loads(text)
            return {
                "overall_score": float(data.get("overall_score", 5.0)),
                "feedback": str(data.get("feedback", "Could not evaluate")),
                "emails_reviewed": int(data.get("emails_reviewed", email_count)),
            }
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("[critic] Failed to parse result JSON: %s", exc)
            return {
                "overall_score": 5.0,
                "feedback": "Could not evaluate",
                "emails_reviewed": email_count,
            }
