# ORIGIN: H-spec — the policy below is Kiel's decisions: the model writes prose only; one retry that
#   says what was wrong (a temperature-0 retry of the same prompt would just repeat itself); no
#   retry on a timeout; any failure leaves the facts untouched and reports why. Lines typed by
#   Claude Code. Edge cases nobody specified are labeled AI where they occur.
"""The only place a model is used: it writes a two-sentence summary of an assembled packet.

The model is a writer at the end of a deterministic pipeline. It may return exactly one thing,
{"summary": "<two sentences>"}, and everything it writes is checked; anything that fails a check is
thrown away and the packet is returned with the summary marked unavailable. The facts, the sources
and the gaps never depend on the model.

This module is the orchestration: prompt.py says what the model is told, ollama.py talks to it,
checks.py decides what may be shown, cache.py remembers finished summaries.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from functools import partial

import httpx

from ..config import Settings
from ..models import ClinicalContextPacket, SummaryBlock, SummaryReason
from .cache import SummaryCache
from .checks import Violation, check_summary
from .ollama import InvalidOutput, ModelUnreachable, ask, request_options
from .prompt import build_prompt, with_correction

logger = logging.getLogger("clinical_context.llm.summarizer")

MAX_ATTEMPTS = 2  # one try, and one retry that says what was wrong


@dataclass(frozen=True)
class SummaryResult:
    block: SummaryBlock
    elapsed_ms: int | None  # wall time of the whole call, retry included; None if never called
    attempts: int
    cached: bool = False  # True when these exact words were remembered from an earlier call


def _unavailable(settings: Settings, reason: SummaryReason) -> SummaryBlock:
    return SummaryBlock(text=None, status="unavailable", model=settings.ollama_model, reason=reason)


def _finished(
    block: SummaryBlock,
    settings: Settings,
    *,
    started: float,
    attempts: int,
    timings: dict[str, int] | None,
) -> SummaryResult:
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "summary outcome=%s cache=miss model=%s attempts=%d elapsed_ms=%d timings=%s",
        block.reason or "generated",
        settings.ollama_model,
        attempts,
        elapsed_ms,
        json.dumps(timings),
    )
    return SummaryResult(block=block, elapsed_ms=elapsed_ms, attempts=attempts)


# ORIGIN: H-spec — Kiel's decision: fail closed. Retry once, and only for a bad answer (invalid
#   JSON or a policy violation). A timeout or an unreachable model is not retried, and one time
#   budget covers the whole call. Whatever goes wrong, the result is "unavailable" plus a reason;
#   it never raises and never returns unchecked text. Lines typed by Claude Code.
async def _generate(
    packet: ClinicalContextPacket,
    system: str,
    user: str,
    *,
    http: httpx.AsyncClient,
    settings: Settings,
) -> SummaryResult:
    started = time.perf_counter()
    attempts = 0
    problem: Violation | None = None  # why the previous attempt was rejected
    timings: dict[str, int] | None = None

    try:
        async with asyncio.timeout(settings.ollama_timeout_seconds):
            for attempt in range(1, MAX_ATTEMPTS + 1):
                attempts = attempt  # kept outside the loop for the log line and a timeout
                prompt = user if problem is None else with_correction(user, problem)
                try:
                    answer = await ask(http, settings, system, prompt)
                except InvalidOutput:
                    problem = Violation.INVALID_JSON
                    continue
                timings = answer.timings
                problem = check_summary(answer.summary, packet, user)
                if problem is None:
                    block = SummaryBlock(
                        text=answer.summary.strip(),
                        status="generated",
                        model=settings.ollama_model,
                        reason=None,
                    )
                    break
            else:  # every attempt was rejected; the last one says why
                reason = (
                    "invalid_output" if problem is Violation.INVALID_JSON else "policy_violation"
                )
                block = _unavailable(settings, reason)
    except (httpx.TimeoutException, TimeoutError):
        block = _unavailable(settings, "timeout")
    except ModelUnreachable as error:
        logger.warning("model unreachable: %s", error)
        block = _unavailable(settings, "model_unreachable")

    return _finished(block, settings, started=started, attempts=attempts, timings=timings)


def _cache_key(settings: Settings, system: str, user: str) -> str:
    """Everything that decides what the model writes: change any of it and the key changes."""
    identity = {
        "model": settings.ollama_model,
        "options": request_options(settings),
        "system": system,
        "user": user,
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _served_from_memory(settings: Settings, block: SummaryBlock) -> SummaryResult:
    logger.info("summary outcome=generated cache=hit model=%s", settings.ollama_model)
    return SummaryResult(block=block, elapsed_ms=0, attempts=0, cached=True)


async def _produce(
    packet: ClinicalContextPacket,
    system: str,
    user: str,
    key: str,
    *,
    http: httpx.AsyncClient,
    settings: Settings,
    cache: SummaryCache | None,
) -> SummaryResult:
    """Wait for the model to be free, generate, and remember the summary if it was finished."""
    async with cache.gate if cache is not None else contextlib.nullcontext():
        result = await _generate(packet, system, user, http=http, settings=settings)
        if cache is not None and result.block.status == "generated":
            cache.put(key, result.block)
        return result


# ORIGIN: AI — added by Claude Code after a probe: with four reviewers at once the fourth waited
#   52 seconds behind the others with no limit, and a reviewer who gave up left the model working
#   for nobody. Now a request waits at most SUMMARY_DEADLINE_SECONDS, queue time included, and then
#   gets its facts with the summary marked unavailable (timeout). When the words can be remembered
#   the generation carries on in the background and lands in the cache, so the next request for that
#   patient has it; a request that is cancelled (a closed tab) does not cancel it either. When
#   nothing can be remembered the call is cancelled at the deadline: nobody would ever read it.
async def summarize(
    packet: ClinicalContextPacket,
    *,
    http: httpx.AsyncClient,
    settings: Settings,
    cache: SummaryCache | None = None,
) -> SummaryResult:
    system, user = build_prompt(packet)
    key = _cache_key(settings, system, user)
    if cache is not None and (remembered := cache.get(key)) is not None:
        return _served_from_memory(settings, remembered)

    started = time.perf_counter()
    produce = partial(
        _produce, packet, system, user, key, http=http, settings=settings, cache=cache
    )
    detached = cache is not None and cache.remembers
    waiting = asyncio.shield(cache.job(key, produce)) if detached else produce()
    try:
        return await asyncio.wait_for(waiting, settings.summary_deadline_seconds)
    except TimeoutError:
        waited_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "summary outcome=timeout cache=%s model=%s waited_ms=%d",
            "continues" if detached else "cancelled",
            settings.ollama_model,
            waited_ms,
        )
        return SummaryResult(
            block=_unavailable(settings, "timeout"), elapsed_ms=waited_ms, attempts=0
        )
