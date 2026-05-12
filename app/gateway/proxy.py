"""
LLM Proxy — executes calls to Anthropic and Groq.

Single responsibility: given a model name + messages, call the right provider
and return a normalized response. No routing logic lives here.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator

import anthropic
from groq import AsyncGroq

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger("proxy")


class NormalizedResponse:
    """Provider-agnostic response envelope."""

    def __init__(self, text: str, input_tokens: int, output_tokens: int, model: str):
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.model = model

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "model": self.model,
        }


def _detect_provider(model: str) -> str:
    """Detect provider from model string prefix."""
    model_lower = model.lower()
    if model_lower.startswith("claude"):
        return "anthropic"
    if model_lower.startswith("llama") or model_lower.startswith("groq/"):
        return "groq"
    raise ValueError(f"Cannot detect provider for model: {model!r}")


def _strip_groq_prefix(model: str) -> str:
    """Strip 'groq/' prefix if present — Groq SDK doesn't want it."""
    if model.lower().startswith("groq/"):
        return model[5:]
    return model


async def call_llm(
    model: str,
    messages: list[dict[str, str]],
    stream: bool = False,
) -> NormalizedResponse:
    """
    Call the appropriate LLM provider.

    Args:
        model:    Model identifier, e.g. 'claude-sonnet-4-20250514' or
                  'llama-3.1-8b-instant' or 'groq/llama-3.1-8b-instant'.
        messages: OpenAI-style message list [{"role": ..., "content": ...}].
        stream:   If True, raises NotImplementedError — use stream_llm() instead.

    Returns:
        NormalizedResponse with text, token counts, and model name.
    """
    if stream:
        raise NotImplementedError("Use stream_llm() for streaming calls.")

    provider = _detect_provider(model)

    if provider == "anthropic":
        return await _call_anthropic(model, messages)
    else:
        return await _call_groq(model, messages)


async def _call_anthropic(model: str, messages: list[dict]) -> NormalizedResponse:
    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    response = await client.messages.create(
        model=model,
        max_tokens=2048,
        messages=messages,
    )
    text = response.content[0].text
    return NormalizedResponse(
        text=text,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        model=model,
    )


async def _call_groq(model: str, messages: list[dict]) -> NormalizedResponse:
    clean_model = _strip_groq_prefix(model)
    client = AsyncGroq(api_key=settings.GROQ_API_KEY, timeout=120.0)
    response = await client.chat.completions.create(
        model=clean_model,
        messages=messages,
        temperature=0.7,
        max_tokens=2048,
    )
    text = response.choices[0].message.content
    usage = response.usage
    return NormalizedResponse(
        text=text,
        input_tokens=usage.prompt_tokens if usage else 0,
        output_tokens=usage.completion_tokens if usage else 0,
        model=clean_model,
    )


async def stream_llm(
    model: str,
    messages: list[dict[str, str]],
) -> AsyncGenerator[str, None]:
    """
    Stream tokens from the LLM.  Only Anthropic and Groq supported.
    Yields raw text chunks.  Caller is responsible for SSE formatting.
    """
    provider = _detect_provider(model)

    if provider == "anthropic":
        async for chunk in _stream_anthropic(model, messages):
            yield chunk
    else:
        async for chunk in _stream_groq(model, messages):
            yield chunk


async def _stream_anthropic(model: str, messages: list[dict]) -> AsyncGenerator[str, None]:
    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    async with client.messages.stream(
        model=model,
        max_tokens=2048,
        messages=messages,
    ) as stream:
        async for text in stream.text_stream:
            yield text


async def _stream_groq(model: str, messages: list[dict]) -> AsyncGenerator[str, None]:
    clean_model = _strip_groq_prefix(model)
    client = AsyncGroq(api_key=settings.GROQ_API_KEY, timeout=120.0)
    stream = await client.chat.completions.create(
        model=clean_model,
        messages=messages,
        temperature=0.7,
        max_tokens=2048,
        stream=True,
    )
    async for chunk in stream:
        delta = chunk.choices[0].delta
        if delta and delta.content:
            yield delta.content
