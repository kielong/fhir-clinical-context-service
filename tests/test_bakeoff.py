# ORIGIN: AI — test cases typed by Claude Code from the agreed bake-off method (same patients, each
#   model cold then warm, JSON validity, latency and tokens per second, environment recorded),
#   reviewed by Kiel. Cases marked (AI) are Claude Code's additions, not yet adopted by Kiel.
"""The bake-off script: measure candidate models with the service's own request and checks."""

import json

import httpx
import pytest
import respx

from bakeoff import (
    ModelRow,
    Run,
    available_models,
    bake_model,
    main,
    summarize_models,
    tokens_per_second,
)
from clinical_context.config import Settings
from ollama_mocks import CHAT, GOOD, NEUTRAL, OLLAMA, reply
from packets import real_packet

API = "http://api.test"
UUID = "2fa15bc7-8866-461a-9000-f739e425860a"
PACKET = real_packet("Aaron697_Brekke496").model_dump(mode="json")
PACKET_URL = f"{API}/v1/patients/{UUID}/clinical-context"
TAGS = f"{OLLAMA}/api/tags"


def _settings(model="llama3.2:3b", **overrides) -> Settings:
    return Settings(
        _env_file=None,
        ollama_host=OLLAMA,
        ollama_model=model,
        ollama_timeout_seconds=5,
        ollama_warmup=False,
        **overrides,
    )


@pytest.fixture
def servers():
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.get(PACKET_URL).respond(200, json=PACKET)
        router.get(f"{OLLAMA}/api/version").respond(200, json={"version": "0.34.2"})
        router.get(f"{OLLAMA}/api/ps").respond(
            200, json={"models": [{"name": "llama3.2:3b", "size": 2_600_000_000, "size_vram": 0}]}
        )
        router.get(TAGS).respond(200, json={"models": [{"name": "llama3.2:3b"}]})
        yield router


def _bodies(route) -> list[dict]:
    return [json.loads(call.request.content) for call in route.calls]


# ---- speed


def test_tokens_per_second_comes_from_ollamas_own_token_count_and_generation_time():
    assert tokens_per_second({"generated_tokens": 48, "generation_ms": 2400}) == pytest.approx(20)


@pytest.mark.parametrize(
    "timings",
    [
        None,
        {},
        {"generated_tokens": 0, "generation_ms": 100},
        {"generated_tokens": 5, "generation_ms": 0},
    ],
)
def test_no_speed_is_reported_when_ollama_did_not_give_the_numbers(timings):
    assert tokens_per_second(timings) is None


# ---- one model, one patient, cold then warm


@pytest.mark.anyio
async def test_the_model_is_unloaded_first_so_the_cold_run_really_includes_loading(servers):
    route = servers.post(CHAT).respond(200, json=reply(NEUTRAL))

    async with httpx.AsyncClient() as http:
        await bake_model(http, _settings(), {"aaron": PACKET})

    first, second, third, *_ = _bodies(route)
    assert first == {"model": "llama3.2:3b", "messages": [], "keep_alive": 0}  # unload
    assert second["messages"] and third["messages"]  # then the cold ask and the warm ask


@pytest.mark.anyio
async def test_each_patient_gets_a_cold_run_and_a_warm_run_recorded_separately(servers):
    servers.post(CHAT).respond(
        200, json=reply(NEUTRAL, total_duration=4_000_000_000, load_duration=1_500_000_000)
    )

    async with httpx.AsyncClient() as http:
        runs = await bake_model(http, _settings(), {"aaron": PACKET})

    assert [(r.patient, r.phase) for r in runs] == [("aaron", "cold"), ("aaron", "warm")]
    cold = runs[0]
    assert (cold.total_ms, cold.load_ms) == (4000, 1500)
    assert cold.outcome == "ok" and cold.json_valid and cold.violation is None
    assert cold.tokens_per_s == pytest.approx(20)  # the fake reply: 48 tokens in 2.4 s
    assert cold.text == NEUTRAL


@pytest.mark.anyio
async def test_the_model_is_unloaded_again_at_the_end_so_it_does_not_crowd_the_next_one(servers):
    route = servers.post(CHAT).respond(200, json=reply(NEUTRAL))

    async with httpx.AsyncClient() as http:
        await bake_model(http, _settings(), {"aaron": PACKET})

    assert _bodies(route)[-1] == {"model": "llama3.2:3b", "messages": [], "keep_alive": 0}


@pytest.mark.anyio
async def test_the_request_is_the_one_the_service_sends(servers):
    # The whole point: numbers must describe what the service would experience.
    route = servers.post(CHAT).respond(200, json=reply(NEUTRAL))

    async with httpx.AsyncClient() as http:
        await bake_model(http, _settings(), {"aaron": PACKET})

    body = _bodies(route)[1]
    assert body["options"]["temperature"] == 0 and body["options"]["seed"] == 0
    assert body["format"]["required"] == ["summary"]
    assert body["stream"] is False


@pytest.mark.anyio
async def test_invalid_json_is_recorded_not_raised(servers):
    servers.post(CHAT).respond(200, json=reply(content="Sure! Here it is"))

    async with httpx.AsyncClient() as http:
        runs = await bake_model(http, _settings(), {"aaron": PACKET})

    assert runs[0].outcome == "invalid_json"
    assert runs[0].json_valid is False
    assert runs[0].text is None


@pytest.mark.anyio
async def test_an_answer_the_service_would_reject_is_recorded_with_the_rule_it_broke(servers):
    servers.post(CHAT).respond(200, json=reply("Approval is recommended. Nothing else."))

    async with httpx.AsyncClient() as http:
        runs = await bake_model(http, _settings(), {"aaron": PACKET})

    assert runs[0].json_valid is True  # the shape was right...
    assert runs[0].violation == "determination_language"  # ...the words were not allowed


@pytest.mark.anyio
async def test_a_model_that_is_not_pulled_is_recorded_as_not_available(servers):
    servers.post(CHAT).respond(404, json={"error": "model not found"})

    async with httpx.AsyncClient() as http:
        runs = await bake_model(http, _settings(), {"aaron": PACKET})

    assert runs[0].outcome == "not_available"


@pytest.mark.anyio
async def test_a_model_too_slow_for_the_timeout_is_recorded_as_a_timeout(servers):
    servers.post(CHAT).mock(side_effect=httpx.ReadTimeout("slow"))

    async with httpx.AsyncClient() as http:
        runs = await bake_model(http, _settings(), {"aaron": PACKET})

    assert runs[0].outcome == "timeout"


@pytest.mark.anyio
async def test_whether_cold_and_warm_gave_the_same_words_is_recorded(servers):
    # (AI) The service found that Ollama's prompt cache can change the wording of one prompt.
    route = servers.post(CHAT)
    route.side_effect = [
        httpx.Response(200, json={}),  # unload
        httpx.Response(200, json=reply("A. First wording.")),
        httpx.Response(200, json=reply("A. Second wording.")),
        httpx.Response(200, json={}),  # unload again
    ]

    async with httpx.AsyncClient() as http:
        runs = await bake_model(http, _settings(), {"aaron": PACKET})

    assert runs[0].text != runs[1].text
    assert [r.same_words_as_cold for r in runs] == [None, False]


# ---- the table


def _run(model, patient, phase, *, total_ms=1000, tps=10.0, text="x", violation=None, valid=True):
    return Run(
        model=model,
        patient=patient,
        phase=phase,
        outcome="ok" if valid else "invalid_json",
        json_valid=valid,
        violation=violation,
        total_ms=total_ms,
        load_ms=0,
        prompt_tokens=300,
        prompt_ms=500,
        generated_tokens=50,
        generation_ms=int(50 / tps * 1000),
        tokens_per_s=tps,
        text=text if valid else None,
        same_words_as_cold=None if phase == "cold" else True,
    )


def test_the_table_has_one_row_per_model_with_medians_and_pass_counts():
    # Uneven values on purpose: a median and a mean must give different answers here.
    runs = [
        _run("a", "p1", "cold", total_ms=9000),
        _run("a", "p1", "warm", total_ms=3000, tps=12),
        _run("a", "p2", "cold", total_ms=11000),
        _run("a", "p2", "warm", total_ms=5000, tps=8),
        _run("a", "p3", "cold", total_ms=40000),
        _run("a", "p3", "warm", total_ms=90000, tps=30),
        _run("b", "p1", "cold", total_ms=20000, valid=False),
        _run("b", "p1", "warm", total_ms=6000, violation="control_claim"),
    ]

    rows = summarize_models(runs)

    a, b = rows
    assert isinstance(a, ModelRow) and (a.model, b.model) == ("a", "b")
    assert (a.json_valid, a.runs) == (6, 6)
    assert (a.passes_checks, a.checked) == (6, 6)
    assert a.cold_ms_median == 11000  # of 9000, 11000, 40000
    assert a.warm_ms_median == 5000  # of 3000, 5000, 90000
    assert a.tokens_per_s_median == pytest.approx(10)  # of 10, 12, 10, 8, 10, 30
    assert a.same_words == "3/3"
    assert (b.json_valid, b.runs) == (1, 2)
    assert (b.passes_checks, b.checked) == (0, 1)  # the invalid one has no words to check


def test_a_model_that_never_answered_still_gets_a_row_with_no_speed():
    rows = summarize_models(
        [
            Run(
                "c",
                "p1",
                "cold",
                "not_available",
                False,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )
        ]
    )

    assert rows[0].cold_ms_median is None and rows[0].tokens_per_s_median is None
    assert rows[0].outcomes == {"not_available": 1}


# ---- which models exist


@pytest.mark.anyio
async def test_the_models_ollama_has_are_listed_by_name(servers):
    async with httpx.AsyncClient() as http:
        assert await available_models(http, OLLAMA) == {"llama3.2:3b"}


# ---- the run


def _main(*argv, settings=None):
    return main(
        [*argv, "--api", API, "--ollama", OLLAMA],
        settings=settings or _settings(),
    )


def test_a_missing_model_stops_the_run_before_anything_is_measured_and_says_how_to_pull_it(
    servers, capsys
):
    chat = servers.post(CHAT).respond(200, json=reply(NEUTRAL))

    code = _main("--model", "llama3.2:3b", "--model", "gemma3:4b", UUID)

    out = capsys.readouterr().out
    assert code == 2
    assert "gemma3:4b" in out and "ollama pull gemma3:4b" in out
    assert chat.call_count == 0


def test_a_run_prints_a_table_and_hides_the_words_unless_asked(servers, capsys):
    servers.post(CHAT).respond(200, json=reply(NEUTRAL))

    code = _main("--model", "llama3.2:3b", UUID)

    out = capsys.readouterr().out
    assert code == 0
    assert "llama3.2:3b" in out and "cold" in out and "tok/s" in out
    assert NEUTRAL not in out


def test_the_words_are_shown_when_asked_for(servers, capsys):
    servers.post(CHAT).respond(200, json=reply(NEUTRAL))

    _main("--model", "llama3.2:3b", UUID, "--show-text")

    assert NEUTRAL in capsys.readouterr().out


def test_everything_measured_is_saved_with_the_environment(servers, tmp_path):
    servers.post(CHAT).respond(200, json=reply(NEUTRAL))
    out = tmp_path / "bakeoff.json"

    _main("--model", "llama3.2:3b", UUID, "--out", str(out), "--environment", "Docker CPU, 8 GB")

    saved = json.loads(out.read_text())
    assert saved["environment"]["note"] == "Docker CPU, 8 GB"
    assert saved["environment"]["ollama_version"] == "0.34.2"
    assert saved["environment"]["processor"] == "CPU"  # from Ollama's own size_vram of 0
    assert [(r["phase"], r["text"]) for r in saved["runs"]] == [
        ("cold", NEUTRAL),
        ("warm", NEUTRAL),
    ]


def test_a_patient_the_api_cannot_find_is_an_error_not_a_measurement(servers, capsys):
    servers.get(f"{API}/v1/patients/{UUID}/clinical-context").respond(404, json={})
    chat = servers.post(CHAT).respond(200, json=reply(GOOD))

    code = _main("--model", "llama3.2:3b", UUID)

    assert code == 2
    assert chat.call_count == 0


def test_a_summary_the_service_is_still_generating_is_waited_for_before_measuring(servers):
    # Otherwise the service and the bake-off would share the model and both would look slow.
    route = servers.get(PACKET_URL)
    timed_out = {
        **PACKET,
        "summary": {**PACKET["summary"], "status": "unavailable", "reason": "timeout"},
    }
    route.side_effect = [httpx.Response(200, json=timed_out), httpx.Response(200, json=PACKET)]
    servers.post(CHAT).respond(200, json=reply(NEUTRAL))

    code = main(
        ["--model", "llama3.2:3b", UUID, "--api", API, "--ollama", OLLAMA, "--pause", "0"],
        settings=_settings(),
    )

    assert code == 0
    assert route.call_count == 2


@pytest.mark.anyio
async def test_a_model_that_times_out_is_not_asked_about_more_patients(servers):
    # A model too slow for one patient will be too slow for the next; each wait is minutes.
    route = servers.post(CHAT).mock(side_effect=httpx.ReadTimeout("slow"))

    async with httpx.AsyncClient() as http:
        runs = await bake_model(http, _settings(), {"aaron": PACKET, "jose": PACKET})

    assert [(r.patient, r.phase, r.outcome) for r in runs] == [("aaron", "cold", "timeout")]
    assert route.call_count == 3  # unload, the one cold ask, and the final unload
