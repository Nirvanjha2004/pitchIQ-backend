"""
Writer Agent — generates personalized cold outreach emails.

Tier routing:
- free user  → tier="free"  → single-pass Groq
- premium user → tier="premium" → cascading: starts with Groq,
  upgrades iterations until score >= 8.5 or max 5 rounds

All other agents always use free tier regardless of user tier.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from app.agents.base import BaseAgent
from app.utils.logger import get_logger

logger = get_logger("writer")

_WRITER_SYSTEM_PROMPT = """You are an expert cold email copywriter specializing in B2B outreach.

Write personalized cold emails for ALL companies listed. Each email must:
- Subject: compelling, specific, under 60 characters
- Body: 3-4 short paragraphs, under 150 words
- Reference the company's specific context, news, or funding
- Address the decision maker by first name (use "Founder" if unknown)
- Clear CTA: 15-minute call
- No generic openers like "I hope this email finds you well"

Return ONLY a JSON array, no preamble, no markdown:
[
  {
    "company": "company name",
    "to": "first name or Founder",
    "subject": "subject line",
    "body": "full email body",
    "personalization_hooks": ["hook1", "hook2"]
  }
]

One object per company. All companies must be included."""


class WriterAgent(BaseAgent):
    """Generates personalized cold outreach emails — batched into one gateway call."""

    def __init__(self, tier: str = "free"):
        super().__init__(name="writer", role="writing", tier=tier)

    async def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generate emails for all companies in ONE gateway call.

        Args:
            task:    Writer instruction from planner.
            context: Must contain 'enricher' or 'researcher' output.

        Returns:
            Dict with 'emails' list.
        """
        logger.info("[writer] Task: %s", task[:80])

        companies = self._get_companies(context)
        if not companies:
            logger.warning("[writer] No companies to write emails for")
            return {"emails": []}

        logger.info("[writer] Writing emails for %d companies (batched)", len(companies))

        # Build one prompt with all companies
        companies_block = self._format_companies(companies)
        prompt = f"""Writing instruction: {task}

Companies and contacts:
{companies_block}

Write one personalized cold email for each company above."""

        messages = [
            {"role": "system", "content": _WRITER_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        try:
            gw = await self._call_gateway(prompt=prompt, messages=messages)
            emails = self._parse_emails(gw.response, companies)
        except Exception as exc:
            logger.warning("[writer] Batch gateway call failed: %s", exc)
            emails = []
        logger.info("[writer] Generated %d emails", len(emails))
        return {"emails": emails}

    def _format_companies(self, companies: List[Dict]) -> str:
        """Format company list for the prompt."""
        parts = []
        for i, c in enumerate(companies, 1):
            dm = c.get("decision_maker_name") or "Founder"
            role = c.get("decision_maker_role") or "Founder"
            parts.append(
                f"{i}. {c.get('company', 'Unknown')}\n"
                f"   Contact: {dm} ({role})\n"
                f"   About: {c.get('description', 'N/A')}\n"
                f"   Funding: {c.get('funding_stage', 'unknown')}\n"
                f"   News: {c.get('recent_news') or 'N/A'}\n"
                f"   Context: {c.get('context_for_email') or 'N/A'}"
            )
        return "\n\n".join(parts)

    def _get_companies(self, context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Get companies — prefer enricher output, fall back to researcher."""
        enricher_output = context.get("enricher", {})
        enriched = enricher_output.get("enriched_companies", [])
        if enriched:
            return enriched

        researcher_output = context.get("researcher", {})
        companies = researcher_output.get("companies", [])
        if companies:
            logger.info("[writer] No enricher output, using researcher companies directly")
            return [
                {
                    "company": c.get("name", ""),
                    "description": c.get("description", ""),
                    "funding_stage": c.get("funding_stage", ""),
                    "recent_news": c.get("recent_news", ""),
                    "website": c.get("website"),
                    "decision_maker_name": None,
                    "decision_maker_role": None,
                    "context_for_email": "",
                }
                for c in companies
            ]
        return []

    def _parse_emails(
        self, raw: str, companies: List[Dict]
    ) -> List[Dict[str, Any]]:
        """Parse JSON — handles array, single object, wrapped object, or partial arrays."""
        text = raw.strip()

        # Strip markdown code fences
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
            text = text.strip()

        def extract_emails(data: Any) -> List[Dict]:
            """Normalize any parsed structure into a list of email dicts."""
            if isinstance(data, list):
                return [e for e in data if isinstance(e, dict) and "body" in e]
            if isinstance(data, dict):
                # Unwrap {"emails": [...]} or {"data": [...]} wrappers
                for key in ("emails", "data", "results", "output"):
                    if key in data and isinstance(data[key], list):
                        return [e for e in data[key] if isinstance(e, dict) and "body" in e]
                # Single email object
                if "body" in data:
                    return [data]
            return []

        # 1. Strict parse
        try:
            result = extract_emails(json.loads(text))
            if result:
                return result
        except (json.JSONDecodeError, ValueError):
            pass

        # 2. Find the outermost JSON array or object in the text
        try:
            arr_match = re.search(r'\[[\s\S]*\]', text)
            if arr_match:
                result = extract_emails(json.loads(arr_match.group()))
                if result:
                    return result
            obj_match = re.search(r'\{[\s\S]*\}', text)
            if obj_match:
                result = extract_emails(json.loads(obj_match.group()))
                if result:
                    return result
        except (json.JSONDecodeError, ValueError):
            pass

        # 3. Use json_repair if available, otherwise manual newline fix
        try:
            # Fix unescaped newlines inside JSON string values — the most
            # common failure mode when the LLM puts literal \n in body text.
            # Strategy: inside string values, replace bare newlines with \\n
            fixed = re.sub(
                r'("(?:[^"\\]|\\.)*")',
                lambda m: m.group(0).replace('\n', '\\n').replace('\r', '\\r'),
                text,
                flags=re.DOTALL,
            )
            result = extract_emails(json.loads(fixed))
            if result:
                return result
        except (json.JSONDecodeError, ValueError):
            pass

        # 4. Last resort: extract individual JSON objects that contain "body"
        # using a more permissive approach that handles nested braces
        try:
            emails = []
            depth = 0
            start = None
            for i, ch in enumerate(text):
                if ch == '{':
                    if depth == 0:
                        start = i
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0 and start is not None:
                        chunk = text[start:i+1]
                        try:
                            # Try fixing newlines in this chunk too
                            fixed_chunk = re.sub(
                                r'("(?:[^"\\]|\\.)*")',
                                lambda m: m.group(0).replace('\n', '\\n').replace('\r', '\\r'),
                                chunk,
                                flags=re.DOTALL,
                            )
                            e = json.loads(fixed_chunk)
                            if isinstance(e, dict) and "body" in e:
                                emails.append(e)
                        except Exception:
                            pass
                        start = None
            if emails:
                return emails
        except Exception:
            pass

        logger.warning(
            "[writer] Could not parse email JSON. Raw response (first 500 chars): %s",
            raw[:500],
        )
        return []
