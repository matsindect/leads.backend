"""OpenAI LLM provider — maps ModelHint to concrete model IDs via config.

Uses the OpenAI async SDK with JSON mode for structured output.
Satisfies ``domain.interfaces.LLMProvider``.
"""

from __future__ import annotations

import json
from typing import Any

import openai
import structlog

from config import Settings
from domain.interfaces import ModelHint

logger = structlog.get_logger()


class OpenAIProvider:
    """Calls the OpenAI Chat Completions API with JSON mode."""

    def __init__(self, settings: Settings) -> None:
        self._client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
        self._model_map = {
            ModelHint.CHEAP: settings.openai_model_cheap,
            ModelHint.SMART: settings.openai_model_smart,
        }
        self._max_tokens = 2048

    async def complete_structured(
        self, prompt: str, schema: dict[str, Any], model_hint: ModelHint
    ) -> dict[str, Any]:
        """Send a prompt and parse the response as JSON.

        Uses ``response_format={"type": "json_object"}`` so the model is
        guaranteed to return valid JSON.  The prompt must mention "JSON"
        for this mode to work (OpenAI requirement).
        """
        model = self._model_map[model_hint]
        log = logger.bind(model=model, hint=model_hint.value)

        response = await self._client.chat.completions.create(
            model=model,
            max_tokens=self._max_tokens,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a structured data extraction assistant."
                        " Always respond with valid JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        )

        choice = response.choices[0]
        raw_text = choice.message.content or ""
        log.debug("llm_response_received", length=len(raw_text))

        result: dict[str, Any] = json.loads(raw_text)

        # Attach token usage for cost tracking (same shape as AnthropicProvider)
        usage = response.usage
        result["_usage"] = {
            "input_tokens": usage.prompt_tokens if usage else 0,
            "output_tokens": usage.completion_tokens if usage else 0,
            "model": model,
        }

        return result
