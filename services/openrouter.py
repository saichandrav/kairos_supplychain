"""Async OpenRouter client for LLM chat completions (OpenAI-compatible API)."""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from agents.ctx import Settings

log = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_RETRIES = 2
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0


class OpenRouterError(Exception):
    """Non-retryable OpenRouter API error."""


class OpenRouterRateLimitError(OpenRouterError):
    """Rate limit hit — caller may retry after backoff."""


async def chat_completion(
    messages: list[dict[str, str]],
    settings: Settings,
    *,
    response_format: dict[str, Any] | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.2,
) -> dict[str, Any]:
    """Call OpenRouter chat completions and return the parsed response.

    Raises OpenRouterError on auth/provider failures, OpenRouterRateLimitError on 429.
    Returns the full API response dict on success.
    """
    api_key = settings.openrouter_api_key
    if not api_key:
        raise OpenRouterError("OPENROUTER_API_KEY is not configured")

    model = settings.openrouter_model or "openai/gpt-4o-mini"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/Bhavesh-Reddy/FlowForge",
        "X-Title": "Kairos Disruption Sensing",
    }
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if response_format:
        payload["response_format"] = response_format

    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=10.0, pool=10.0),
            ) as client:
                resp = await client.post(OPENROUTER_BASE_URL, headers=headers, json=payload)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 401:
                raise OpenRouterError("Authentication failed — check OPENROUTER_API_KEY")
            if resp.status_code == 429:
                if attempt <= MAX_RETRIES:
                    log.warning("OpenRouter rate-limited (attempt %d/%d)", attempt, MAX_RETRIES + 1)
                    import asyncio
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise OpenRouterRateLimitError("Rate limit exceeded after retries")
            if resp.status_code >= 500:
                if attempt <= MAX_RETRIES:
                    log.warning("OpenRouter server error %d (attempt %d/%d)", resp.status_code, attempt, MAX_RETRIES + 1)
                    import asyncio
                    await asyncio.sleep(1)
                    continue

            body = resp.text[:500]
            raise OpenRouterError(f"OpenRouter returned {resp.status_code}: {body}")

        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
            last_exc = exc
            if attempt <= MAX_RETRIES:
                log.warning("OpenRouter network error (attempt %d/%d): %s", attempt, MAX_RETRIES + 1, exc)
                import asyncio
                await asyncio.sleep(1)
                continue
            raise OpenRouterError(f"Network error after {MAX_RETRIES + 1} attempts: {exc}") from exc

    raise OpenRouterError(f"Exhausted retries: {last_exc}")


def extract_content(response: dict[str, Any]) -> str:
    """Extract the assistant message content from an OpenRouter response."""
    try:
        return response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenRouterError(f"Malformed response structure: {exc}") from exc


def extract_json(response: dict[str, Any]) -> dict[str, Any]:
    """Extract and parse JSON from the assistant message content."""
    raw = extract_content(response)
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = lines[1:]  # drop opening ```json
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise OpenRouterError(f"Model returned invalid JSON: {exc}; raw={raw[:300]}") from exc
