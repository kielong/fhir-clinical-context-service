# ORIGIN: AI — test cases typed by Claude Code from the agreed warm-up behavior, reviewed by Kiel.
"""Loading the model in the background at startup: it must help, and never get in the way."""

import asyncio
import time

import httpx
from fastapi.testclient import TestClient

from clinical_context.config import Settings, get_settings
from clinical_context.main import app
from ollama_mocks import CHAT, OLLAMA


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

    import json

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
