"""
Researcher Agent — web search + summarization using Tavily.

Runs 2-3 targeted searches based on planner instruction.
Summarizes findings into structured company context.
Uses FREE tier gateway (extraction task, cheap model fine).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from app.agents.base import BaseAgent
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger("researcher")

_SUMMARIZE_PROMPT = """You are a research analyst. Given raw search results, extract structured company information.

IMPORTANT: Only extract companies that are explicitly mentioned in the search results below. Do NOT invent or hallucinate company names. If a field is not found in the results, use null.

Return ONLY this JSON, no preamble, no markdown:
{
  "companies": [
    {
      "name": "exact company name from search results",
      "description": "what they do in 1-2 sentences, from search results",
      "funding_stage": "seed/series-a/series-b/unknown — only if mentioned",
      "recent_news": "most relevant recent news from search results, or null",
      "website": "website url if found, or null"
    }
  ],
  "raw_summary": "2-3 sentence overall summary of findings"
}

Extract up to 5 companies. Only include companies with at least a name and description found in the results."""


class ResearcherAgent(BaseAgent):
    """Searches the web and summarizes findings into structured company data."""

    def __init__(self, tier: str = "free"):
        super().__init__(name="researcher", role="research", tier="agent")
        self._tavily = None

    def _get_tavily(self):
        """Lazy-load Tavily client."""
        if self._tavily is None:
            from tavily import TavilyClient
            self._tavily = TavilyClient(api_key=settings.TAVILY_API_KEY)
        return self._tavily

    async def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Run targeted searches and return structured company data.
        """
        logger.info("[researcher] Task: %s", task[:80])

        # Extract requested count from instruction (e.g. "find 3 startups" → 3)
        import re as _re
        count_match = _re.search(r'\b(\d+)\b', task)
        requested_count = int(count_match.group(1)) if count_match else 5
        requested_count = max(1, min(requested_count, 10))  # clamp 1-10

        queries = self._build_queries(task)
        logger.info("[researcher] Running %d searches (want %d companies): %s", len(queries), requested_count, queries)

        # Emit live search events if task_id available via context
        task_id = context.get("_task_id")

        all_results: List[str] = []
        for query in queries:
            try:
                if task_id:
                    from app.services.event_emitter import emit_agent_log
                    emit_agent_log(task_id, "researcher", f"Searching: {query[:60]}")
                results = self._get_tavily().search(query, max_results=5)
                for r in results.get("results", []):
                    snippet = f"[{r.get('title', '')}] {r.get('content', '')[:300]} (source: {r.get('url', '')})"
                    all_results.append(snippet)
                logger.info("[researcher] Query '%s' → %d results", query[:50], len(results.get("results", [])))
            except Exception as exc:
                logger.warning("[researcher] Search failed for '%s': %s", query[:50], exc)

        if not all_results:
            logger.warning("[researcher] No search results found")
            return {"companies": [], "raw_summary": "No results found for the given research task."}

        combined = "\n\n".join(all_results[:15])
        prompt = f"""Research instruction: {task}
Requested number of companies: {requested_count}

Search results:
{combined}

Extract exactly {requested_count} companies (or fewer if not enough found). Do NOT include more than {requested_count}."""

        messages = [
            {"role": "system", "content": _SUMMARIZE_PROMPT},
            {"role": "user", "content": prompt},
        ]

        gw = await self._call_gateway(prompt=prompt, messages=messages)
        output = self._parse_output(gw.response)

        # Hard cap to requested count
        if len(output.get("companies", [])) > requested_count:
            output["companies"] = output["companies"][:requested_count]

        logger.info("[researcher] Found %d companies (requested %d)", len(output.get("companies", [])), requested_count)
        return output

    def _build_queries(self, instruction: str) -> List[str]:
        """
        Build 2-3 short, targeted search queries from the planner instruction.

        Strips tool-specific language (Tavily, web search, etc.) and extracts
        the core search intent. Hard cap at 100 chars.
        """
        import re

        # Strip tool/meta language the planner sometimes includes
        cleaned = re.sub(
            r'\b(use|using|via|with|search|tavily|web search|google|return \d+ results?|limit results? to \d+)\b',
            ' ',
            instruction,
            flags=re.IGNORECASE,
        )

        # Take first sentence / clause as the core intent
        core = cleaned.split(".")[0].split("\n")[0].split(",")[0].strip()
        core = re.sub(r'\s+', ' ', core).strip()

        # Hard cap at 100 chars, cut at word boundary
        if len(core) > 100:
            core = core[:100].rsplit(" ", 1)[0]

        if not core:
            core = instruction[:80]

        queries = [core]
        queries.append(f"{core[:75]} funding 2024 2025")
        queries.append(f"{core[:75]} recent news")

        return queries[:3]

    def _parse_output(self, raw: str) -> Dict[str, Any]:
        """Parse researcher JSON output with fallback."""
        import re
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if len(lines) > 2 else text

        try:
            data = json.loads(text)
            if "companies" not in data:
                raise ValueError("Missing 'companies' key")
            return data
        except (json.JSONDecodeError, ValueError):
            pass

        try:
            cleaned = re.sub(r'[\x00-\x1f\x7f]', lambda m: repr(m.group())[1:-1], text)
            data = json.loads(cleaned)
            if "companies" in data:
                return data
        except (json.JSONDecodeError, ValueError):
            pass

        logger.warning("[researcher] Failed to parse output, using raw text")
        return {"companies": [], "raw_summary": raw[:500]}
