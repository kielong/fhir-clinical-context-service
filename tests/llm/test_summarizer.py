# ORIGIN: AI — test cases typed by Claude Code from the agreed summarizer policy, reviewed by Kiel.
"""The summarizer as a whole: a good answer, a bad answer, and no answer."""

import asyncio
import json
import logging

import httpx
import pytest

from ollama_mocks import CHAT, GOOD, reply, run, sent
from packets import real_packet

pytestmark = pytest.mark.anyio


async def test_a_good_answer_becomes_a_generated_summary(ollama):
    route = ollama.post(CHAT).respond(200, json=reply())

    result = await run(real_packet("Aaron697_Brekke496"))

    assert result.block.status == "generated"
    assert result.block.text == GOOD
    assert result.block.model == "llama3.2:3b"
    assert result.block.reason is None
    assert route.call_count == 1
    assert result.attempts == 1


async def test_the_answer_is_trimmed_of_surrounding_whitespace(ollama):
    ollama.post(CHAT).respond(200, json=reply(f"  {GOOD}\n"))

    result = await run(real_packet("Aaron697_Brekke496"))

    assert result.block.text == GOOD


async def test_ollamas_own_timings_are_logged_in_milliseconds_and_not_part_of_the_result(
    ollama, caplog
):
    caplog.set_level(logging.INFO, logger="clinical_context")
    ollama.post(CHAT).respond(
        200,
        json=reply(
            total_duration=4_500_000_000,
            load_duration=1_500_000_000,
            prompt_eval_count=210,
            prompt_eval_duration=900_000_000,
            eval_count=48,
            eval_duration=2_400_000_000,
        ),
    )

    result = await run(real_packet("Aaron697_Brekke496"))

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert (
        '"total_ms": 4500, "load_ms": 1500, "prompt_tokens": 210, "prompt_ms": 900, '
        '"generated_tokens": 48, "generation_ms": 2400' in logged
    )
    assert isinstance(result.elapsed_ms, int) and result.elapsed_ms >= 0
    assert not hasattr(result, "timings")


async def test_invalid_json_gets_one_corrective_retry_then_succeeds(ollama):
    route = ollama.post(CHAT)
    route.side_effect = [
        httpx.Response(200, json=reply(content="Sure! Here you go: two sentences")),
        httpx.Response(200, json=reply()),
    ]

    result = await run(real_packet("Aaron697_Brekke496"))

    assert result.block.status == "generated"
    assert result.attempts == 2
    first, second = sent(route, 0), sent(route, 1)
    # A temperature-0 retry with the same prompt would repeat the same answer, so the retry
    # says what was wrong.
    assert second["messages"][1]["content"].startswith(first["messages"][1]["content"])
    assert "Your previous answer was rejected because" in second["messages"][1]["content"]
    assert "valid JSON" in second["messages"][1]["content"]
    assert first["messages"][1]["content"] != second["messages"][1]["content"]


async def test_invalid_json_twice_is_unavailable_with_reason_invalid_output(ollama):
    route = ollama.post(CHAT).respond(200, json=reply(content="not json"))

    result = await run(real_packet("Aaron697_Brekke496"))

    assert result.block.status == "unavailable"
    assert result.block.text is None
    assert result.block.reason == "invalid_output"
    assert result.block.model == "llama3.2:3b"  # still recorded
    assert route.call_count == 2  # one try and one retry, never more


@pytest.mark.parametrize(
    "content",
    [
        json.dumps({"answer": GOOD}),  # wrong field
        json.dumps({"summary": GOOD, "source": "Condition/1"}),  # an extra field
        json.dumps({"summary": 42}),  # not a string
        json.dumps([GOOD]),  # not an object
        '{"summary": "cut off mid-sen',  # truncated at num_predict
    ],
)
async def test_json_that_is_not_exactly_a_summary_string_is_invalid_output(ollama, content):
    ollama.post(CHAT).respond(200, json=reply(content=content))

    result = await run(real_packet("Aaron697_Brekke496"))

    assert result.block.status == "unavailable"
    assert result.block.reason == "invalid_output"


@pytest.mark.parametrize(
    "bad",
    [
        "Recommend approval of the request. Coverage is warranted.",
        "Their diabetes is well controlled. No medications are recorded.",
        "See Condition/123. Nothing else is recorded.",
    ],
)
async def test_a_policy_violation_twice_is_unavailable_with_reason_policy_violation(ollama, bad):
    route = ollama.post(CHAT).respond(200, json=reply(bad))

    result = await run(real_packet("Aaron697_Brekke496"))

    assert result.block.status == "unavailable"
    assert result.block.text is None  # the bad text is thrown away, never shown
    assert result.block.reason == "policy_violation"
    assert route.call_count == 2


async def test_a_policy_violation_then_a_clean_answer_is_generated_and_the_retry_names_the_rule(
    ollama,
):
    route = ollama.post(CHAT)
    route.side_effect = [
        httpx.Response(200, json=reply("Approval is recommended. Nothing else.")),
        httpx.Response(200, json=reply()),
    ]

    result = await run(real_packet("Aaron697_Brekke496"))

    assert result.block.status == "generated"
    assert "determination" in sent(route, 1)["messages"][1]["content"]
    assert "Approval is recommended" not in sent(route, 1)["messages"][1]["content"]  # not echoed


async def test_a_summary_never_changes_the_packet_it_was_written_for(ollama):
    ollama.post(CHAT).respond(200, json=reply())
    packet = real_packet("Aaron697_Brekke496")
    before = packet.model_dump_json()

    await run(packet)

    assert packet.model_dump_json() == before


async def test_a_timeout_is_unavailable_with_reason_timeout_and_is_not_retried(ollama):
    route = ollama.post(CHAT).mock(side_effect=httpx.ReadTimeout("slow"))

    result = await run(real_packet("Aaron697_Brekke496"))

    assert result.block.status == "unavailable"
    assert result.block.reason == "timeout"
    assert route.call_count == 1  # a slow model would only be slower the second time


async def test_the_whole_call_including_any_retry_shares_one_time_budget(ollama):
    started = []

    async def slow(request):
        started.append(request)  # respx only counts calls that finish, and this one is cancelled
        await asyncio.sleep(2)
        return httpx.Response(200, json=reply())

    ollama.post(CHAT).mock(side_effect=slow)

    result = await run(real_packet("Aaron697_Brekke496"), ollama_timeout_seconds=0.2)

    assert result.block.reason == "timeout"
    assert len(started) == 1  # it was not retried


async def test_ollama_not_running_is_unavailable_with_reason_model_unreachable(ollama):
    route = ollama.post(CHAT).mock(side_effect=httpx.ConnectError("refused"))

    result = await run(real_packet("Aaron697_Brekke496"))

    assert result.block.status == "unavailable"
    assert result.block.reason == "model_unreachable"
    assert route.call_count == 1


@pytest.mark.parametrize("status", [404, 500, 503])
async def test_ollama_answering_with_an_error_status_is_model_unreachable(ollama, status):
    # 404 is what Ollama says when the model has not been pulled.
    route = ollama.post(CHAT).respond(status, json={"error": "model not found"})

    result = await run(real_packet("Aaron697_Brekke496"))

    assert result.block.reason == "model_unreachable"
    assert route.call_count == 1


async def test_a_failure_never_produces_text(ollama):
    ollama.post(CHAT).mock(side_effect=httpx.ConnectError("refused"))

    result = await run(real_packet("Aaron697_Brekke496"))

    assert result.block.text is None
    assert result.elapsed_ms is not None  # we did try, so we can say how long it took


async def test_a_summary_that_contradicts_the_record_is_retried_then_rejected(ollama):
    route = ollama.post(CHAT).respond(
        200, json=reply("A young man with obesity and no reported allergies.")
    )

    result = await run(real_packet("Andreas188_Dare640"))  # five active allergies

    assert result.block.status == "unavailable"
    assert result.block.reason == "policy_violation"
    assert result.block.text is None
    assert route.call_count == 2
    assert "contradicted" in sent(route, 1)["messages"][1]["content"]


async def test_a_contradiction_then_a_correct_answer_is_generated(ollama):
    route = ollama.post(CHAT)
    route.side_effect = [
        httpx.Response(200, json=reply("A young man with no reported allergies.")),
        httpx.Response(200, json=reply("A young man with obesity and several recorded allergies.")),
    ]

    result = await run(real_packet("Andreas188_Dare640"))

    assert result.block.status == "generated"
    assert "allergies" in result.block.text


async def test_an_invented_number_is_retried_then_rejected_and_the_retry_names_the_rule(ollama):
    route = ollama.post(CHAT).respond(
        200, json=reply("A 57-year-old male with recorded anemia and prediabetes.")
    )

    result = await run(real_packet("Aaron697_Brekke496"))  # he is 73

    assert result.block.status == "unavailable"
    assert result.block.reason == "policy_violation"
    assert route.call_count == 2
    assert "number or date" in sent(route, 1)["messages"][1]["content"]


async def test_an_invented_number_then_a_faithful_answer_is_generated(ollama):
    route = ollama.post(CHAT)
    route.side_effect = [
        httpx.Response(200, json=reply("Diagnosed with anemia in 2015. Nothing else is recorded.")),
        httpx.Response(200, json=reply("A 73-year-old male with recorded anemia and prediabetes.")),
    ]

    result = await run(real_packet("Aaron697_Brekke496"))

    assert result.block.status == "generated"
    assert "73-year-old" in result.block.text
