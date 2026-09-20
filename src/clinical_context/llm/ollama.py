# ORIGIN: H-spec — Kiel's decision: decoding is pinned so the same prompt gives the same text.
#   temperature 0 and a fixed seed are the decision; top_k 1 (always take the single most likely
#   token, so nothing is sampled at all) is Claude Code's addition to close the remaining gap.
#   Lines typed by Claude Code.
"""Talking to Ollama: one structured chat request, and the warm-up that loads the model.

The request pins the decoding (temperature 0, top_k 1, a fixed seed, fixed context and output
sizes) and constrains the output to a one-field JSON schema, so the model can only answer with
{"summary": "<text>"}.
"""

import asyncio
import logging
from collections.abc import Sequence
from typing import NamedTuple

import httpx
from pydantic import BaseModel, ConfigDict

from ..config import Settings

logger = logging.getLogger("clinical_context.llm.ollama")


class _SummaryOnly(BaseModel):
    """The one thing the model may return."""

    model_config = ConfigDict(extra="forbid")
    summary: str


RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}

SEED = 0


def request_options(settings: Settings) -> dict:
    return {
        "temperature": 0,
        "top_k": 1,
        "seed": SEED,
        "num_ctx": settings.ollama_num_ctx,
        "num_predict": settings.ollama_num_predict,
    }


class Answer(NamedTuple):
    summary: str
    timings: dict[str, int] | None  # Ollama's own duration and token counts, converted to ms


class ModelUnreachable(Exception):
    """Ollama did not answer, or answered with an error (including 'model not found')."""


class InvalidOutput(Exception):
    """Ollama answered, but not with exactly {"summary": <string>}."""


# ORIGIN: AI — edge cases added by Claude Code: any error status from Ollama (a 404 means the model
#   is not pulled) counts as "unreachable", and a truncated or wrong-shaped body is "invalid".
async def ask(http: httpx.AsyncClient, settings: Settings, system: str, user: str) -> Answer:
    body = {
        "model": settings.ollama_model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        "format": RESPONSE_SCHEMA,
        "options": request_options(settings),
        "keep_alive": settings.ollama_keep_alive,
    }
    try:
        response = await http.post(
            f"{settings.ollama_host}/api/chat", json=body, timeout=settings.ollama_timeout_seconds
        )
    except httpx.TimeoutException:
        raise  # a slow model is reported as a timeout, not as unreachable
    except httpx.HTTPError as error:
        raise ModelUnreachable(type(error).__name__) from error
    if response.status_code != 200:
        raise ModelUnreachable(f"HTTP {response.status_code}")
    try:
        data = response.json()
        summary = _SummaryOnly.model_validate_json(data["message"]["content"]).summary
    except (ValueError, KeyError, TypeError) as error:
        raise InvalidOutput from error
    return Answer(summary, _timings(data))


_TIMING_FIELDS = {  # our name: (Ollama's field, divisor from nanoseconds)
    "total_ms": ("total_duration", 1_000_000),
    "load_ms": ("load_duration", 1_000_000),
    "prompt_tokens": ("prompt_eval_count", 1),
    "prompt_ms": ("prompt_eval_duration", 1_000_000),
    "generated_tokens": ("eval_count", 1),
    "generation_ms": ("eval_duration", 1_000_000),
}


def _timings(data: dict) -> dict[str, int] | None:
    found = {
        name: data[field] // divisor
        for name, (field, divisor) in _TIMING_FIELDS.items()
        if isinstance(data.get(field), int)
    }
    return found or None


# Pauses between warm-up attempts, in seconds: about 90 in all, then it gives up.
WARMUP_DELAYS = (1, 2, 4, 8, 15, 30, 30)


# ORIGIN: AI — added by Claude Code: Ollama unloads an idle model after a few minutes, and loading
#   a 3B model is what stalls the first request. An empty chat request loads it ahead of time.
#   docker compose starts Ollama and the API together, so Ollama is often not up yet (or is still
#   pulling the model, which answers 404): a probe showed a single try just failed and left the
#   first reviewer to pay for the cold load. So it retries with growing pauses, then gives up. It
#   must never stop the service from starting, so a failure is only logged.
async def warm_up(
    http: httpx.AsyncClient, settings: Settings, delays: Sequence[float] | None = None
) -> None:
    pauses = WARMUP_DELAYS if delays is None else delays
    problem = ""
    for attempt, pause in enumerate([*pauses, None], start=1):
        try:
            response = await http.post(
                f"{settings.ollama_host}/api/chat",
                json={
                    "model": settings.ollama_model,
                    "messages": [],
                    "keep_alive": settings.ollama_keep_alive,
                },
                timeout=settings.ollama_timeout_seconds,
            )
            if response.status_code == 200:
                logger.info(
                    "model warm-up finished model=%s attempts=%d", settings.ollama_model, attempt
                )
                return
            problem = f"HTTP {response.status_code}"
        except Exception as error:  # deliberately broad: warm-up is best effort
            problem = type(error).__name__
        if pause is not None:
            logger.info(
                "model warm-up attempt %d failed (%s); retrying in %ss", attempt, problem, pause
            )
            await asyncio.sleep(pause)
    logger.warning("model warm-up gave up after %d attempts: %s", len(pauses) + 1, problem)
