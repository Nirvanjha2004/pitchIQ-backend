"""
Enricher Agent — finds decision maker info for each company.

Batches all companies into ONE Tavily search + ONE gateway call
instead of N calls (one per company). This cuts enricher time from
N×20s to ~10s total regardless of company count.

Key optimisation: Tavily searches run concurrently via asyncio.gather,
not sequentially. 10 companies × 3 results used to take ~60s serially;
concurrent execution brings it down to the latency of the slowest single
search (~3-6s).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from app.agents.base import BaseAgent
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger("enricher")

_ENRICHER_PROMPT = """You are a contact enrichment specialist. Given search results about multiple companies' leadership, extract decision maker information for each company.

Return ONLY a JSON array, no preamble, no markdown:
[
  {
    "company": "exact company name",
    "decision_maker_name": "full name or null",
    "decision_maker_role": "founder/CEO/CTO/etc or null",
    "linkedin_url": "LinkedIn URL or null",
    "context_for_email": "1 sentence about this person relevant for cold outreach"
  }
]

Include one entry per company. If no decision maker found for a company, use null for those fields."""


class EnricherAgent(BaseAgent):
    """Finds decision maker names and contact info for companies — batched."""

    def __init__(self, tier: str = "free"):
        super().__init__(name="enricher", role="enrichment", tier="agent")
        self._tavily = None

    def _get_tavily(self):
        if self._tavily is None:
            from tavily import TavilyClient
            self._tavily = TavilyClient(api_key=settings.TAVILY_API_KEY)
        return self._tavily

    async def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Enrich all companies in one batched gateway call.

        Args:
            task:    Enricher instruction from planner.
            context: Must contain 'researcher' output with 'companies' list.

        Returns:
            Dict with 'enriched_companies' list.
        """
        logger.info("[enricher] Task: %s", task[:80])

        researcher_output = context.get("researcher", {})
        companies = researcher_output.get("companies", [])

        if not companies:
            logger.warning("[enricher] No companies to enrich")
            return {"enriched_companies": []}

        logger.info("[enricher] Enriching %d companies (concurrent Tavily searches)", len(companies))

        task_id = context.get("_task_id")

        # ── Step 1: All Tavily searches run concurrently ─────────────────────
        all_snippets = await self._search_all_companies(companies, task_id)

        if not all_snippets:
            logger.warning("[enricher] No search results, returning companies without enrichment")
            return {
                "enriched_companies": [self._empty_enriched(c) for c in companies]
            }

        # ── Step 2: ONE gateway call to extract all decision makers ──────────
        company_names = [c.get("name", "") for c in companies if c.get("name")]
        combined = "\n\n".join(all_snippets[:20])  # cap tokens

        prompt = (
            f"Companies to enrich: {', '.join(company_names)}\n\n"
            f"Search results:\n{combined}\n\n"
            f"Extract decision maker info for each company listed above."
        )

        messages = [
            {"role": "system", "content": _ENRICHER_PROMPT},
            {"role": "user", "content": prompt},
        ]

        try:
            gw = await self._call_gateway(prompt=prompt, messages=messages)
            dm_list = self._parse_batch(gw.response, companies)
        except Exception as exc:
            logger.warning("[enricher] Batch gateway call failed: %s", exc)
            dm_list = [self._empty_enriched(c) for c in companies]

        # ── Step 3: Merge with researcher company data ────────────────────────
        dm_map = {item["company"].lower(): item for item in dm_list}
        enriched: List[Dict[str, Any]] = []

        for company in companies:
            name = company.get("name", "")
            dm = dm_map.get(name.lower(), {})
            enriched.append({
                "company": name,
                "description": company.get("description", ""),
                "funding_stage": company.get("funding_stage", "unknown"),
                "recent_news": company.get("recent_news", ""),
                "website": company.get("website"),
                "decision_maker_name": dm.get("decision_maker_name"),
                "decision_maker_role": dm.get("decision_maker_role"),
                "linkedin_url": dm.get("linkedin_url"),
                "context_for_email": dm.get("context_for_email", ""),
            })

        logger.info("[enricher] Enriched %d companies", len(enriched))
        return {"enriched_companies": enriched}

    async def _search_company(
        self, name: str, task_id: Optional[str]
    ) -> List[str]:
        """Run a single Tavily search for one company. Returns snippet strings."""
        query = f"{name} founder CEO LinkedIn"
        try:
            if task_id:
                from app.services.event_emitter import emit_agent_log
                emit_agent_log(task_id, "enricher", f"Looking up: {name}")
            # Tavily client is sync — run in thread pool to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(
                None,
                lambda: self._get_tavily().search(query, max_results=3),
            )
            snippets = []
            for r in results.get("results", []):
                snippet = f"[{name}] {r.get('title', '')} — {r.get('content', '')[:150]}"
                snippets.append(snippet)
            return snippets
        except Exception as exc:
            logger.warning("[enricher] Search failed for %s: %s", name, exc)
            return []

    async def _search_all_companies(
        self, companies: List[Dict], task_id: Optional[str]
    ) -> List[str]:
        """Run all company searches concurrently and flatten results."""
        tasks = [
            self._search_company(c.get("name", ""), task_id)
            for c in companies
            if c.get("name")
        ]
        results: List[List[str]] = await asyncio.gather(*tasks)
        # Flatten
        return [snippet for company_snippets in results for snippet in company_snippets]

    def _parse_batch(
        self, raw: str, companies: List[Dict]
    ) -> List[Dict[str, Any]]:
        """Parse batch JSON array with fallback."""
        import re

        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if len(lines) > 2 else text

        try:
            data = json.loads(text)
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, ValueError):
            pass

        try:
            cleaned = re.sub(r'[\x00-\x1f\x7f]', lambda m: repr(m.group())[1:-1], text)
            data = json.loads(cleaned)
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, ValueError):
            pass

        logger.warning("[enricher] Failed to parse batch JSON, returning empty enrichment")
        return [self._empty_enriched(c) for c in companies]

    def _empty_enriched(self, company: Dict) -> Dict[str, Any]:
        return {
            "company": company.get("name", ""),
            "decision_maker_name": None,
            "decision_maker_role": None,
            "linkedin_url": None,
            "context_for_email": "",
        }
