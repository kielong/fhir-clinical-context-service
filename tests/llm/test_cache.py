# ORIGIN: AI — test cases typed by Claude Code from the agreed summarizer policy, reviewed by Kiel.
"""Same packet, same words: finished summaries are remembered and model calls are serialized."""

import asyncio

import httpx
import pytest

import synthetic as syn
from clinical_context.llm.cache import SummaryCache
from ollama_mocks import CHAT, NEUTRAL, reply, run
from packets import packet_from, real_packet

pytestmark = pytest.mark.anyio


ANSWER_A = (
    "A 73-year-old male has recorded active anemia and prediabetes. No medications are recorded."
)
ANSWER_B = "The patient has anemia and prediabetes on record. There are no medications on file."


async def test_the_words_stay_the_same_even_if_the_model_would_word_them_differently_next_time(
    ollama,
):
    # Ollama's prompt cache changes the numerics: the same prompt can come back worded differently
    # depending on what ran before it, even at temperature 0. So the finished summary is
    # remembered under the exact input that produced it, and the same packet gets the same words.
    route = ollama.post(CHAT)
    route.side_effect = [
        httpx.Response(200, json=reply(ANSWER_A)),
        httpx.Response(200, json=reply(ANSWER_B)),
    ]
    cache = SummaryCache(8)
    packet = real_packet("Aaron697_Brekke496")

    first = await run(packet, cache=cache)
    second = await run(packet, cache=cache)

    assert first.block.text == second.block.text == ANSWER_A
    assert route.call_count == 1  # the second answer was never even asked for


async def test_without_a_cache_the_same_packet_is_generated_each_time(ollama):
    route = ollama.post(CHAT).respond(200, json=reply())
    packet = real_packet("Aaron697_Brekke496")

    await run(packet)
    await run(packet)

    assert route.call_count == 2


async def test_a_remembered_summary_is_marked_as_cached_and_costs_no_attempts(ollama):
    ollama.post(CHAT).respond(200, json=reply())
    cache = SummaryCache(8)
    packet = real_packet("Aaron697_Brekke496")

    first = await run(packet, cache=cache)
    second = await run(packet, cache=cache)

    assert (first.cached, second.cached) == (False, True)
    assert second.attempts == 0
    assert second.block == first.block


async def test_a_different_packet_is_summarized_separately(ollama):
    route = ollama.post(CHAT).respond(200, json=reply(NEUTRAL))
    cache = SummaryCache(8)

    await run(real_packet("Aaron697_Brekke496"), cache=cache)
    await run(real_packet("Jose871_Williamson769"), cache=cache)

    assert route.call_count == 2


async def test_a_change_in_the_packet_is_a_change_in_the_key(ollama):
    # The key is the whole prompt, so new or changed facts can never return an old summary.
    route = ollama.post(CHAT).respond(200, json=reply())
    cache = SummaryCache(8)
    before = packet_from(patient=syn.patient("1"), conditions=[syn.condition("c1")])
    after = packet_from(
        patient=syn.patient("1"),
        conditions=[syn.condition("c1"), syn.condition("c2", display="Asthma")],
    )

    await run(before, cache=cache)
    await run(after, cache=cache)

    assert route.call_count == 2


@pytest.mark.parametrize(
    "different_setting",
    [{"ollama_model": "phi4-mini:3.8b"}, {"ollama_num_ctx": 2048}, {"ollama_num_predict": 100}],
)
async def test_a_different_model_or_decoding_setting_is_a_different_key(ollama, different_setting):
    # An answer from one model must never be served as if another model wrote it.
    route = ollama.post(CHAT).respond(200, json=reply())
    cache = SummaryCache(8)
    packet = real_packet("Aaron697_Brekke496")

    await run(packet, cache=cache)
    await run(packet, cache=cache, **different_setting)

    assert route.call_count == 2


async def test_only_finished_summaries_are_remembered_never_failures(ollama):
    route = ollama.post(CHAT)
    route.side_effect = [httpx.ConnectError("down"), httpx.Response(200, json=reply())]
    cache = SummaryCache(8)
    packet = real_packet("Aaron697_Brekke496")

    failed = await run(packet, cache=cache)
    recovered = await run(packet, cache=cache)  # Ollama is back: it must be asked again

    assert failed.block.status == "unavailable"
    assert recovered.block.status == "generated"


async def test_a_rejected_answer_is_not_remembered(ollama):
    route = ollama.post(CHAT).respond(200, json=reply("Approval is recommended. Nothing else."))
    cache = SummaryCache(8)
    packet = real_packet("Aaron697_Brekke496")

    await run(packet, cache=cache)
    await run(packet, cache=cache)

    assert route.call_count == 4  # two attempts each time: nothing was served from memory


async def test_the_oldest_summary_is_forgotten_first_when_the_cache_is_full(ollama):
    route = ollama.post(CHAT).respond(200, json=reply(NEUTRAL))
    cache = SummaryCache(2)
    a, b, c = (
        real_packet(n) for n in ("Aaron697_Brekke496", "Jose871_Williamson769", "Alan320_Wiza601")
    )

    await run(a, cache=cache)
    await run(b, cache=cache)
    await run(c, cache=cache)  # pushes out `a`
    calls_before = route.call_count
    await run(b, cache=cache)  # still remembered
    await run(a, cache=cache)  # forgotten: asked again

    assert route.call_count - calls_before == 1


async def test_a_cache_of_size_zero_remembers_nothing(ollama):
    route = ollama.post(CHAT).respond(200, json=reply())
    cache = SummaryCache(0)
    packet = real_packet("Aaron697_Brekke496")

    await run(packet, cache=cache)
    await run(packet, cache=cache)

    assert route.call_count == 2


async def test_simultaneous_identical_requests_make_one_model_call(ollama):
    calls = []

    async def slow(request):
        calls.append(request)
        await asyncio.sleep(0.1)
        return httpx.Response(200, json=reply())

    ollama.post(CHAT).mock(side_effect=slow)
    cache = SummaryCache(8)
    packet = real_packet("Aaron697_Brekke496")

    first, second = await asyncio.gather(run(packet, cache=cache), run(packet, cache=cache))

    assert len(calls) == 1
    assert first.block == second.block


async def test_the_model_is_asked_for_one_summary_at_a_time(ollama):
    # Ollama batches concurrent requests together, which is one more thing that can change the
    # words. One at a time also matches a CPU-only model, which cannot do them in parallel anyway.
    active = peak = 0

    async def tracked(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return httpx.Response(200, json=reply(NEUTRAL))

    ollama.post(CHAT).mock(side_effect=tracked)
    cache = SummaryCache(8)
    names = ("Aaron697_Brekke496", "Jose871_Williamson769", "Alan320_Wiza601")

    await asyncio.gather(*(run(real_packet(n), cache=cache) for n in names))

    assert peak == 1
