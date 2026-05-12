"""Tests for agent system"""

import pytest
from app.agents.planner import PlannerAgent
from app.agents.writer import WriterAgent

@pytest.mark.asyncio
async def test_planner_agent():
    """Test planner agent"""
    agent = PlannerAgent()
    # TODO: Mock LLM responses
    pass

@pytest.mark.asyncio
async def test_writer_agent():
    """Test writer agent"""
    agent = WriterAgent()
    # TODO: Mock LLM responses
    pass

@pytest.mark.asyncio
async def test_agent_memory():
    """Test agent memory"""
    agent = PlannerAgent()
    agent.remember("test_key", "test_value")
    assert agent.get_memory("test_key") == "test_value"
