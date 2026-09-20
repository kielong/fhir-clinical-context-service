# ORIGIN: AI — test cases typed by Claude Code from the agreed summarizer policy, reviewed by Kiel.
"""The summarizer: what the model is told, what it may answer, and what happens when it fails.

Ollama is faked with respx, so no model runs. The point is the policy around the model: it can
only ever add a checked two-sentence summary to a packet, and any failure leaves the facts alone.
"""

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

import synthetic as syn
from clinical_context.config import Settings
from clinical_context.summarizer import (
    SYSTEM_PROMPT,
    SummaryCache,
    build_prompt,
    contradicts_facts,
    strip_semantic_tag,
    summarize,
    validate_summary,
)
from ollama_mocks import CHAT, GOOD, NEUTRAL, OLLAMA, reply
from packets import busy_synthetic_packet, packet_from, real_packet

pytestmark = pytest.mark.anyio


def _settings(**overrides) -> Settings:
    defaults = {
        "ollama_host": OLLAMA,
        "ollama_model": "llama3.2:3b",
        "ollama_timeout_seconds": 5,
        "ollama_warmup": False,
    }
    return Settings(_env_file=None, **{**defaults, **overrides})


async def run(packet, *, cache=None, **settings_overrides):
    async with httpx.AsyncClient() as http:
        return await summarize(
            packet, http=http, settings=_settings(**settings_overrides), cache=cache
        )


def sent(route, index: int = -1) -> dict:
    return json.loads(route.calls[index].request.content)


# ============================================================ the prompt


def test_the_system_prompt_forbids_invention_ids_determinations_and_control_claims():
    rules = SYSTEM_PROMPT
    assert "exactly two plain sentences" in rules
    assert "not listed" in rules  # never add a fact
    assert "identifier" in rules and "patient's name" in rules
    assert "approved, denied, or authorized" in rules
    assert "medically necessary" in rules
    assert "controlled, stable, improving, or worsening" in rules
    assert 'Write "recorded as active", never "currently has"' in rules
    assert "deceased" in rules
    assert '{"summary"' in rules  # the only thing it may answer with


def test_the_user_prompt_has_age_and_gender_and_the_facts_real():
    _, user = build_prompt(real_packet("Aaron697_Brekke496"))

    assert user.startswith("Patient: 73-year-old male.")
    for label in ("Anemia", "Prediabetes", "Body mass index 30+ - obesity", "Cardiac Arrest"):
        assert f"- {label}\n" in user or user.endswith(f"- {label}")
    assert "Conditions (recorded active):" in user
    assert "Medications (recorded active or on hold):\n- none" in user
    assert "Allergies:\n- none" in user


def test_the_user_prompt_never_contains_ids_names_or_resource_names():
    packet = real_packet("Aaron697_Brekke496")
    system, user = build_prompt(packet)
    prompt = system + user

    for private in ("Aaron", "Brekke", "Patient/", "Condition/", "MedicationRequest/"):
        assert private not in prompt
    for fact in (*packet.conditions, *packet.medications, *packet.allergies):
        assert fact.source.split("/")[1] not in prompt  # no resource id, however it is spelled
    assert packet.patient.fhir_id not in user
    assert "MedicationRequest" not in user and "AllergyIntolerance" not in user  # gaps are plain


def test_snomed_semantic_tags_are_dropped_from_the_prompt_but_not_the_packet():
    packet = real_packet("Aaron697_Brekke496")

    _, user = build_prompt(packet)

    assert "(disorder)" not in user and "(finding)" not in user and "(situation)" not in user
    assert "Anemia (disorder)" in [c.display for c in packet.conditions]  # the packet is untouched


def test_only_known_semantic_tags_are_stripped():
    assert strip_semantic_tag("Anemia (disorder)") == "Anemia"
    assert strip_semantic_tag("Body mass index 30+ - obesity (finding)") == (
        "Body mass index 30+ - obesity"
    )
    assert (
        strip_semantic_tag("History of cardiac arrest (situation)") == "History of cardiac arrest"
    )
    # A meaningful parenthetical is not a SNOMED tag and must survive.
    assert strip_semantic_tag("Hypertension (high blood pressure)") == (
        "Hypertension (high blood pressure)"
    )
    assert strip_semantic_tag("Prediabetes") == "Prediabetes"


def test_a_deceased_patient_is_described_as_deceased_with_last_recorded_statuses_real():
    _, user = build_prompt(real_packet("Floyd420_Jerde200"))

    assert user.startswith("Patient: male, deceased at age 95.")
    assert "Statuses below are the last recorded, not current treatment." in user


def test_a_living_patient_prompt_says_nothing_about_death():
    _, user = build_prompt(real_packet("Aaron697_Brekke496"))

    assert "deceased" not in user and "last recorded" not in user


def test_unknown_age_and_gender_are_stated_not_invented():
    packet = packet_from(patient=syn.patient("1", birth=None, gender=None))

    _, user = build_prompt(packet)

    assert user.startswith("Patient: age unknown.")


def test_gaps_are_written_in_plain_language_with_the_excluded_counts():
    _, user = build_prompt(real_packet("Aaron697_Brekke496"))

    assert (
        "- No active or on-hold medications are recorded (1 other is recorded as no longer active)"
        in user
    )
    assert "- No allergies are recorded" in user


def test_a_truncated_list_and_unreadable_records_are_mentioned_in_plain_language():
    packet = packet_from(
        patient=syn.patient("1"),
        conditions=[syn.condition(f"c{i}", onset=f"2000-01-0{i + 1}") for i in range(4)],
        medications=[syn.medication(None)],
        list_cap=3,
    )

    _, user = build_prompt(packet)

    assert "Only the most recent conditions are listed" in user
    assert "Some medication records could not be read" in user


def test_allergy_criticality_and_on_hold_status_are_shown():
    packet = packet_from(
        patient=syn.patient("1"),
        medications=[syn.medication("m1", status="on-hold", drug="Warfarin 5 MG")],
        allergies=[syn.allergy("a1", criticality="high", display="Penicillin allergy")],
    )

    _, user = build_prompt(packet)

    assert "- Warfarin 5 MG (on hold)" in user
    assert "- Penicillin allergy (criticality: high)" in user


def test_the_prompt_is_identical_every_time_it_is_built():
    packet = real_packet("Jose871_Williamson769")

    assert build_prompt(packet) == build_prompt(packet)


def test_the_prompt_does_not_depend_on_ids_or_the_clock():
    # Anything that varies between runs would make the model's answer vary too.
    first = real_packet(
        "Aaron697_Brekke496",
        patient_id_echo="2fa15bc7",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    second = real_packet(
        "Aaron697_Brekke496", patient_id_echo="1000", generated_at=datetime(2030, 6, 6, tzinfo=UTC)
    )
    second = second.model_copy(update={"meta": second.meta.model_copy(update={"timings_ms": None})})

    assert build_prompt(first) == build_prompt(second)


def test_the_largest_possible_packet_fits_far_inside_the_context_window():
    system, user = build_prompt(busy_synthetic_packet(25))

    # Roughly 4 characters per token: 6,000 characters is about 1,500 tokens of a 4,096 window.
    assert len(system) + len(user) < 6000


# ============================================================ what the model may answer


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (GOOD, None),
        ("One short sentence.", None),
        ("", "empty"),
        ("   ", "empty"),
        ("x" * 401, "too_long"),
        ("First sentence. Second sentence. Third sentence.", "too_many_sentences"),
        ("See Condition/123 for details.", "contains_identifier"),
        ("Recorded under MedicationRequest/abc.", "contains_identifier"),
        ("Patient 2fa15bc7-8866-461a-9000-f739e425860a is recorded.", "contains_identifier"),
        ("Recommend approval of the request.", "determination_language"),
        ("The claim should be denied.", "determination_language"),
        ("Request denied.", "determination_language"),
        ("This is authorized care.", "determination_language"),
        ("Treatment is medically necessary.", "determination_language"),
        ("There is medical necessity here.", "determination_language"),
        ("The patient is eligible for coverage.", "determination_language"),
        ("Their diabetes is well controlled.", "control_claim"),
        ("Their diabetes is well-controlled.", "control_claim"),
        ("Blood pressure is poorly controlled.", "control_claim"),
        ("The condition is uncontrolled.", "control_claim"),
        ("The patient is stable.", "control_claim"),
        ("Symptoms are improving.", "control_claim"),
        ("Symptoms are worsening.", "control_claim"),
    ],
)
def test_validate_summary_table(text, expected):
    assert validate_summary(text) == expected


def test_decimals_and_abbreviations_do_not_count_as_extra_sentences():
    text = "Recorded active on metformin 0.5 g daily, with hypertension. No allergies are recorded."

    assert validate_summary(text) is None


def test_words_that_only_contain_a_forbidden_stem_inside_another_word_are_allowed():
    # "denies" is clinical language (a patient denies pain), and "stability" is not "stable".
    assert validate_summary("The record notes the patient denies chest pain.") is None


# ============================================================ a successful call


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


async def test_timings_come_from_ollamas_own_duration_fields_in_milliseconds(ollama):
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

    assert result.timings == {
        "total_ms": 4500,
        "load_ms": 1500,
        "prompt_tokens": 210,
        "prompt_ms": 900,
        "generated_tokens": 48,
        "generation_ms": 2400,
    }
    assert isinstance(result.elapsed_ms, int) and result.elapsed_ms >= 0


# ============================================================ making it deterministic


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
    packet = real_packet("Jose871_Williamson769")

    await run(packet)
    await run(packet)

    assert route.calls[0].request.content == route.calls[1].request.content


async def test_the_model_is_never_shown_a_source_or_asked_to_write_one(ollama):
    route = ollama.post(CHAT).respond(200, json=reply())

    await run(real_packet("Aaron697_Brekke496"))

    assert "source" not in sent(route)["format"]["properties"]
    assert "source" not in json.dumps(sent(route)["messages"]).lower().replace("resource", "")


# ============================================================ fail closed: bad output


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


# ============================================================ fail closed: no answer


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


# ============================================================ same packet, same words


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


# ============================================================ never contradict the record


def _packet_with(*, conditions=(), medications=(), allergies=()):
    return packet_from(
        patient=syn.patient("1"),
        conditions=list(conditions),
        medications=list(medications),
        allergies=list(allergies),
    )


ALLERGIC = _packet_with(allergies=[syn.allergy("a1", display="Shellfish allergy")])
ON_MEDS = _packet_with(medications=[syn.medication("m1")])
HAS_CONDITIONS = _packet_with(conditions=[syn.condition("c1")])
EMPTY = _packet_with()


@pytest.mark.parametrize(
    ("packet", "text"),
    [
        # Found live: a 3B model told a reviewer "no reported allergies" for a patient with five.
        (ALLERGIC, "A 23-year-old male with obesity, with no reported allergies."),
        (ALLERGIC, "The patient has no recorded allergies."),
        (ALLERGIC, "There are no known allergies. Nothing else."),
        (ALLERGIC, "Gaps in the record include no active allergies."),
        (ALLERGIC, "Seen without any documented allergies."),
        (ON_MEDS, "There are no active or on-hold medications."),
        (ON_MEDS, "The patient has no recorded medications."),
        (HAS_CONDITIONS, "The chart has no recorded conditions."),
        (HAS_CONDITIONS, "There are no active diagnoses on file."),
    ],
)
def test_saying_none_is_recorded_when_the_record_lists_some_is_a_contradiction(packet, text):
    assert contradicts_facts(text, packet) == "contradicts_facts"


@pytest.mark.parametrize(
    ("packet", "text"),
    [
        (EMPTY, "The patient has no recorded allergies and no active medications."),  # true
        (ALLERGIC, "The patient has a recorded shellfish allergy."),
        (ALLERGIC, "There are no active conditions, but a shellfish allergy is recorded."),
        (ON_MEDS, "The patient has no recorded allergies."),  # this packet really has none
        (HAS_CONDITIONS, "Recorded active type 2 diabetes; no medications are recorded."),
        (ALLERGIC, "No further details are recorded."),
    ],
)
def test_true_statements_of_absence_and_unrelated_wording_are_allowed(packet, text):
    assert contradicts_facts(text, packet) is None


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
