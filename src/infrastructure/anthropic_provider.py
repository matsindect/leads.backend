"""Anthropic LLM provider — maps ModelHint to concrete model IDs via config.

Satisfies ``domain.interfaces.LLMProvider``.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
import structlog

from config import Settings
from domain.interfaces import ModelHint

logger = structlog.get_logger()


class AnthropicProvider:
    """Calls the Anthropic Messages API with structured JSON output."""

    def __init__(self, settings: Settings) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._model_map = {
            ModelHint.CHEAP: settings.llm_model_cheap,
            ModelHint.SMART: settings.llm_model_smart,
        }
        self._max_tokens = 2048

    async def complete_structured(
        self, prompt: str, schema: dict[str, Any], model_hint: ModelHint
    ) -> dict[str, Any]:
        """Send a prompt and parse the response as JSON.

        Returns the parsed dict. Raises on invalid JSON or API errors.
        """
        model = self._model_map[model_hint]
        log = logger.bind(model=model, hint=model_hint.value)

        response = await self._client.messages.create(
            model=model,
            max_tokens=self._max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )

        raw_text = response.content[0].text
        log.debug("llm_response_received", length=len(raw_text))

        # Extract JSON from response (handle markdown code blocks)
        json_str = raw_text.strip()
        if json_str.startswith("```"):
            lines = json_str.split("\n")
            json_str = "\n".join(lines[1:-1])

        result: dict[str, Any] = json.loads(json_str)

        # Attach token usage for cost tracking
        result["_usage"] = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "model": model,
        }

        return result
