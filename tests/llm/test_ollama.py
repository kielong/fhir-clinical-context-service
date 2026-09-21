# ORIGIN: AI — test cases typed by Claude Code from the agreed summarizer policy, reviewed by Kiel.
"""The request Ollama receives: pinned decoding and a one-field structured output."""

import json

import pytest

from clinical_context.llm.prompt import build_prompt
from ollama_mocks import CHAT, NEUTRAL, reply, run, sent
from packets import real_packet

pytestmark = pytest.mark.anyio


async def test_the_request_asks_for_greedy_decoding_a_fixed_seed_and_only_a_summary_field(ollama):
    route = ollama.post(CHAT).respond(200, json=reply())
    packet = real_packet("Aaron697_Brekke496")

    await run(packet, ollama_num_ctx=4096, ollama_num_predict=160, ollama_keep_alive="30m")

    body = sent(route)
    system, user = build_prompt(packet)
    assert route.calls.last.request.url.path == "/api/chat"
    assert body["model"] == "llama3.2:3b"
    assert body["stream"] is False
    assert body["messages"] == [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    # Temperature 0 + top_k 1 = always the single most likely next token (greedy); the seed pins
    # anything that would still be sampled. Together they make the same prompt give the same text.
    assert body["options"] == {
        "temperature": 0,
        "top_k": 1,
        "seed": 0,
        "num_ctx": 4096,
        "num_predict": 160,
    }
    assert body["keep_alive"] == "30m"
    # Structured output: the model can only produce {"summary": <string>}, nothing else.
    assert body["format"]["type"] == "object"
    assert list(body["format"]["properties"]) == ["summary"]
    assert body["format"]["properties"]["summary"] == {"type": "string"}
    assert body["format"]["required"] == ["summary"]
    assert body["format"]["additionalProperties"] is False


async def test_the_same_packet_sends_byte_identical_requests(ollama):
    route = ollama.post(CHAT).respond(200, json=reply(NEUTRAL))
    packet = real_packet("Shelly431_Corwin846")

    await run(packet)
    await run(packet)

    assert route.calls[0].request.content == route.calls[1].request.content


async def test_the_model_is_never_shown_a_source_or_asked_to_write_one(ollama):
    route = ollama.post(CHAT).respond(200, json=reply())

    await run(real_packet("Aaron697_Brekke496"))

    assert "source" not in sent(route)["format"]["properties"]
    assert "source" not in json.dumps(sent(route)["messages"]).lower().replace("resource", "")
