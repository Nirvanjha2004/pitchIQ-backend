"""Tests for inference gateway"""

import pytest
from app.gateway.router import ModelRouter

def test_simple_query_routing():
    """Test routing for simple queries"""
    model = ModelRouter.route("small context", "short query")
    # Should route to groq for simple queries
    assert model == ModelRouter.SIMPLE_MODEL

def test_complex_query_routing():
    """Test routing for complex queries"""
    large_context = "\n".join(["line"] * 10)
    model = ModelRouter.route(large_context, "x" * 300)
    # Should route to claude for complex queries
    assert model == ModelRouter.COMPLEX_MODEL

@pytest.mark.asyncio
async def test_llm_proxy():
    """Test LLM proxy calls"""
    # TODO: Mock LLM API calls
    pass
