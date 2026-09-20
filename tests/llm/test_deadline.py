# ORIGIN: AI — test cases typed by Claude Code from the agreed deadline behavior, reviewed by Kiel.
"""A request waits a bounded time for a summary; the model keeps working for the next request.

On a CPU-only model a summary can take tens of seconds, and requests queue behind one another
(one model call at a time). A reviewer must not wait behind that queue indefinitely, and work the
model has already started should not be thrown away: it finishes in the background and its words
are remembered for the next request.
"""

import asyncio
import contextlib
import logging
from functools import partial

import httpx
import pytest

from clinical_context.config import Settings
from clinical_context.llm import summarizer
from clinical_context.llm.cache import SummaryCache
from ollama_mocks import CHAT, GOOD, NEUTRAL, reply
from ollama_mocks import run as summarize_with
from packets import real_packet

pytestmark = pytest.mark.anyio


@pytest.fixture
async def run():
    """Summarize with one long-lived client, like the service: a background generation must be
    able to outlive the request that started it, which a client closed per call would prevent."""
    async with httpx.AsyncClient() as http:
        yield partial(summarize_with, http=http)


QUICK = {"summary_deadline_seconds": 0.1}


class SlowModel:
    """A fake Ollama that answers only when the test says so, and records what it was asked."""

    def __init__(self, ollama, answer: str = NEUTRAL) -> None:
        self.started: list[httpx.Request] = []
        self.finished: list[httpx.Request] = []
        self.release = asyncio.Event()
        self._answer = answer
        ollama.post(CHAT).mock(side_effect=self._respond)

    async def _respond(self, request: httpx.Request) -> httpx.Response:
        self.started.append(request)
        await self.release.wait()  # a cancelled call never gets past here
        self.finished.append(request)
        return httpx.Response(200, json=reply(self._answer))

    async def let_go_and_settle(self, calls: int = 1) -> None:
        self.release.set()
        for _ in range(200):
            if len(self.finished) >= calls:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)  # the finished words are written to the cache just after


async def until(condition, seconds: float = 2) -> None:
    for _ in range(int(seconds / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("gave up waiting")


AARON = "Aaron697_Brekke496"
JOSE = "Jose871_Williamson769"


def test_a_request_waits_forty_five_seconds_for_a_summary_unless_told_otherwise():
    assert Settings(_env_file=None).summary_deadline_seconds == 45


async def test_a_request_stops_waiting_at_the_deadline_and_says_so(run, ollama):
    SlowModel(ollama)

    result = await run(real_packet(AARON), cache=SummaryCache(8), **QUICK)

    assert result.block.status == "unavailable"
    assert result.block.reason == "timeout"
    assert result.block.text is None
    assert 90 <= result.elapsed_ms < 1000  # it waited about the deadline, not the model's time


async def test_the_model_keeps_working_and_the_next_request_gets_the_words(run, ollama):
    model = SlowModel(ollama)
    cache = SummaryCache(8)
    packet = real_packet(AARON)

    first = await run(packet, cache=cache, **QUICK)
    await model.let_go_and_settle()
    second = await run(packet, cache=cache, **QUICK)

    assert first.block.reason == "timeout"
    assert second.cached is True
    assert second.block.status == "generated"
    assert second.block.text == NEUTRAL
    assert len(model.started) == 1  # one model call served both requests


async def test_a_request_that_arrives_while_the_model_is_still_working_joins_it(run, ollama):
    model = SlowModel(ollama)
    cache = SummaryCache(8)
    packet = real_packet(AARON)

    first = await run(packet, cache=cache, **QUICK)  # gave up
    waiting = asyncio.create_task(run(packet, cache=cache, summary_deadline_seconds=5))
    await asyncio.sleep(0.05)
    model.release.set()
    second = await waiting

    assert first.block.reason == "timeout"
    assert second.block.status == "generated"
    assert len(model.started) == 1  # it did not start a second generation


async def test_time_spent_waiting_in_the_queue_counts_against_the_deadline(run, ollama):
    model = SlowModel(ollama)
    cache = SummaryCache(8)
    ahead = asyncio.create_task(run(real_packet(AARON), cache=cache, summary_deadline_seconds=5))
    await until(lambda: len(model.started) == 1)  # the first request holds the model

    queued = await run(real_packet(JOSE), cache=cache, **QUICK)

    assert queued.block.reason == "timeout"
    assert len(model.started) == 1  # the queued request never even reached the model
    await model.let_go_and_settle(calls=2)
    await ahead
    assert (await run(real_packet(JOSE), cache=cache, **QUICK)).cached is True  # and it still ran


async def test_a_request_that_is_cancelled_does_not_cancel_the_generation(run, ollama):
    # A reviewer closing the tab must not waste a generation another reviewer will ask for.
    model = SlowModel(ollama)
    cache = SummaryCache(8)
    packet = real_packet(AARON)
    request = asyncio.create_task(run(packet, cache=cache, summary_deadline_seconds=5))
    await until(lambda: len(model.started) == 1)

    request.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await request
    await model.let_go_and_settle()

    assert len(model.finished) == 1
    assert (await run(packet, cache=cache, **QUICK)).cached is True


@pytest.mark.parametrize("cache_factory", [lambda: None, lambda: SummaryCache(0)])
async def test_when_nothing_can_be_remembered_the_deadline_cancels_the_call(
    run, ollama, cache_factory
):
    # With no memory to fill, a finished summary would help nobody; stop paying for it.
    model = SlowModel(ollama)

    result = await run(real_packet(AARON), cache=cache_factory(), **QUICK)
    model.release.set()
    await asyncio.sleep(0.1)

    assert result.block.reason == "timeout"
    assert len(model.started) == 1
    assert model.finished == []  # the call was cancelled, not left running


async def test_closing_the_cache_cancels_generations_still_running(run, ollama):
    model = SlowModel(ollama)
    cache = SummaryCache(8)
    await run(real_packet(AARON), cache=cache, **QUICK)  # gave up; the job is still running

    async with asyncio.timeout(1):  # cancelled, not waited out (the model would take 5 s)
        await cache.close()
    model.release.set()
    await asyncio.sleep(0.1)

    assert model.finished == []
    async with asyncio.timeout(1):
        async with cache.gate:  # the model gate was released, not left held by a dead job
            pass


async def test_a_bug_inside_the_job_reaches_the_caller_and_is_logged_without_its_message(
    run, ollama, monkeypatch, caplog
):
    async def broken(*args, **kwargs):
        raise RuntimeError("secret patient detail")

    monkeypatch.setattr(summarizer, "_generate", broken)
    caplog.set_level(logging.INFO, logger="clinical_context")

    with pytest.raises(RuntimeError):
        await run(real_packet(AARON), cache=SummaryCache(8))
    await asyncio.sleep(0.05)

    assert "RuntimeError" in caplog.text
    assert "secret patient detail" not in caplog.text


async def test_a_crashed_job_is_forgotten_so_the_next_request_can_try_again(
    run, ollama, monkeypatch
):
    calls = []

    async def broken_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("bug")
        return await real_generate(*args, **kwargs)

    real_generate = summarizer._generate
    ollama.post(CHAT).respond(200, json=reply(GOOD))
    monkeypatch.setattr(summarizer, "_generate", broken_once)
    cache = SummaryCache(8)
    packet = real_packet(AARON)

    with pytest.raises(RuntimeError):
        await run(packet, cache=cache)
    retry = await run(packet, cache=cache)

    assert retry.block.status == "generated"


async def test_the_deadline_wait_is_logged_without_patient_data(run, ollama, caplog):
    SlowModel(ollama)
    caplog.set_level(logging.INFO, logger="clinical_context")

    await run(real_packet(AARON), cache=SummaryCache(8), **QUICK)

    assert "outcome=timeout" in caplog.text
    assert "Aaron" not in caplog.text and "Anemia" not in caplog.text
