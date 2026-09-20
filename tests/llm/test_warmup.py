# ORIGIN: AI — test cases typed by Claude Code from the agreed warm-up behavior, reviewed by Kiel.
"""Loading the model in the background at startup: it must help, and never get in the way."""

import asyncio
import json
import logging
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from clinical_context.config import Settings, get_settings
from clinical_context.llm import ollama
from clinical_context.llm.cache import SummaryCache
from clinical_context.llm.ollama import warm_up
from clinical_context.main import app
from ollama_mocks import CHAT, OLLAMA, settings


def _start(hapi_router, *, warmup: bool = True):
    settings = Settings(
        _env_file=None,
        ollama_host=OLLAMA,
        ollama_model="llama3.2:3b",
        ollama_keep_alive="30m",
        ollama_warmup=warmup,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, raise_server_exceptions=False)


def test_the_warmup_asks_ollama_to_load_the_configured_model_and_keep_it(hapi):
    route = hapi.post(CHAT).respond(200, json={"done": True})

    with _start(hapi):
        deadline = time.time() + 3
        while not route.called and time.time() < deadline:
            time.sleep(0.02)
    app.dependency_overrides.clear()

    body = json.loads(route.calls.last.request.content)
    # An empty chat request loads a model without generating anything.
    assert body == {"model": "llama3.2:3b", "messages": [], "keep_alive": "30m"}


def test_a_slow_model_load_never_delays_startup(hapi):
    started = []

    async def very_slow(request):
        started.append(request)
        await asyncio.sleep(30)  # a cold load on a slow machine
        return httpx.Response(200, json={"done": True})

    hapi.post(CHAT).mock(side_effect=very_slow)

    began = time.perf_counter()
    with _start(hapi):
        startup_seconds = time.perf_counter() - began
    app.dependency_overrides.clear()

    assert startup_seconds < 2  # the app was ready long before the model was
    assert started  # ...and the load really was requested


def test_a_failed_warmup_does_not_stop_the_service_from_starting(hapi):
    hapi.post(CHAT).mock(side_effect=httpx.ConnectError("ollama is not running"))

    with _start(hapi) as client:
        assert client.get("/openapi.json").status_code == 200  # up and serving
    app.dependency_overrides.clear()


def test_warmup_can_be_switched_off(hapi):
    route = hapi.post(CHAT).respond(200, json={"done": True})

    with _start(hapi, warmup=False):
        time.sleep(0.2)
    app.dependency_overrides.clear()

    assert not route.called


def test_shutdown_stops_running_summary_jobs_before_the_http_client_closes(hapi, monkeypatch):
    closed = []
    real_close = SummaryCache.close

    async def spy(self):
        closed.append(True)
        await real_close(self)

    monkeypatch.setattr(SummaryCache, "close", spy)

    with _start(hapi, warmup=False):
        assert closed == []  # still running while the app serves
    app.dependency_overrides.clear()

    assert closed == [True]


# ---- Ollama may start after the API (docker compose starts them together), so warm-up retries


@pytest.fixture
def quiet_backoff(monkeypatch):
    monkeypatch.setattr(ollama, "WARMUP_DELAYS", (0, 0, 0))


@pytest.mark.anyio
async def test_warmup_retries_until_ollama_answers(hapi):
    route = hapi.post(CHAT)
    route.side_effect = [
        httpx.ConnectError("not up yet"),
        httpx.ConnectError("not up yet"),
        httpx.Response(200, json={"done": True}),
    ]

    async with httpx.AsyncClient() as http:
        await warm_up(http, settings(), delays=(0, 0, 0))

    assert route.call_count == 3  # and it stopped as soon as one worked


@pytest.mark.anyio
@pytest.mark.parametrize("status", [404, 500, 503])
async def test_warmup_retries_when_the_model_is_not_ready_yet(hapi, status):
    # 404 is what Ollama says while the model is still being pulled.
    route = hapi.post(CHAT)
    route.side_effect = [httpx.Response(status, json={"error": "x"}), httpx.Response(200, json={})]

    async with httpx.AsyncClient() as http:
        await warm_up(http, settings(), delays=(0, 0))

    assert route.call_count == 2


@pytest.mark.anyio
async def test_a_warmup_that_never_succeeds_gives_up_and_never_raises(hapi, caplog):
    route = hapi.post(CHAT).mock(side_effect=httpx.ConnectError("down"))
    caplog.set_level(logging.INFO, logger="clinical_context")

    async with httpx.AsyncClient() as http:
        await warm_up(http, settings(), delays=(0, 0))

    assert route.call_count == 3  # the first try and one per delay
    assert "gave up after 3 attempts" in caplog.text
    assert "ConnectError" in caplog.text


@pytest.mark.anyio
async def test_a_warmup_that_works_the_first_time_is_not_repeated(hapi):
    route = hapi.post(CHAT).respond(200, json={"done": True})

    async with httpx.AsyncClient() as http:
        await warm_up(http, settings(), delays=(0, 0))

    assert route.call_count == 1


@pytest.mark.anyio
async def test_shutdown_stops_a_warmup_that_is_waiting_to_retry(hapi):
    route = hapi.post(CHAT).mock(side_effect=httpx.ConnectError("down"))

    async with httpx.AsyncClient() as http:
        task = asyncio.create_task(warm_up(http, settings(), delays=(30,)))
        await asyncio.sleep(0.05)  # it failed once and is now waiting out the pause
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert route.call_count == 1


def test_the_running_service_retries_the_warmup_when_ollama_starts_late(hapi, quiet_backoff):
    route = hapi.post(CHAT)
    route.side_effect = [httpx.ConnectError("not up yet"), httpx.Response(200, json={"done": True})]

    with _start(hapi):
        deadline = time.time() + 3
        while route.call_count < 2 and time.time() < deadline:
            time.sleep(0.02)
    app.dependency_overrides.clear()

    assert route.call_count == 2
