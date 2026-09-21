# ORIGIN: AI — test cases drafted and typed by Claude Code, reviewed by Kiel
"""/health reports each backend separately and never fails the request.

No network: the probe client is an httpx.AsyncClient on a MockTransport.
"""

import httpx
from fastapi.testclient import TestClient

from clinical_context.config import Settings, get_settings
from clinical_context.dependencies import get_http
from clinical_context.main import app

FHIR_BASE = "http://hapi.test/fhir"
OLLAMA_HOST = "http://ollama.test"
METADATA_URL = f"{FHIR_BASE}/metadata"
TAGS_URL = f"{OLLAMA_HOST}/api/tags"

DOWN = "down"  # sentinel: the backend refuses connections


def _settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        fhir_base_url=FHIR_BASE,
        ollama_host=OLLAMA_HOST,
        ollama_model=overrides.pop("ollama_model", "llama3.2:3b"),
        **overrides,
    )


def _client(routes: dict[str, httpx.Response | str]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        outcome = routes.get(str(request.url))
        if outcome == DOWN:
            raise httpx.ConnectError("connection refused", request=request)
        if outcome is None:
            return httpx.Response(404)
        return outcome

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _get_health(routes: dict[str, httpx.Response | str], **settings_overrides) -> httpx.Response:
    client = _client(routes)
    app.dependency_overrides[get_http] = lambda: client
    app.dependency_overrides[get_settings] = lambda: _settings(**settings_overrides)
    try:
        return TestClient(app).get("/health")
    finally:
        app.dependency_overrides.clear()


def _ok_metadata() -> httpx.Response:
    return httpx.Response(200, json={"resourceType": "CapabilityStatement"})


def _tags(*names: str) -> httpx.Response:
    return httpx.Response(200, json={"models": [{"name": name} for name in names]})


def test_all_backends_up():
    response = _get_health({METADATA_URL: _ok_metadata(), TAGS_URL: _tags("llama3.2:3b")})

    assert response.status_code == 200
    assert response.json() == {"hapi": "ok", "ollama_http": "ok", "ollama_model": "ok"}


def test_ollama_http_up_but_model_not_pulled():
    response = _get_health({METADATA_URL: _ok_metadata(), TAGS_URL: _tags()})

    assert response.status_code == 200
    assert response.json() == {"hapi": "ok", "ollama_http": "ok", "ollama_model": "missing"}


def test_other_models_present_but_configured_model_missing():
    response = _get_health({METADATA_URL: _ok_metadata(), TAGS_URL: _tags("gemma3:4b")})

    assert response.json()["ollama_http"] == "ok"
    assert response.json()["ollama_model"] == "missing"


def test_hapi_down_is_reported_but_request_still_200():
    response = _get_health({METADATA_URL: DOWN, TAGS_URL: _tags("llama3.2:3b")})

    assert response.status_code == 200
    assert response.json() == {"hapi": "down", "ollama_http": "ok", "ollama_model": "ok"}


def test_hapi_error_status_counts_as_down():
    response = _get_health(
        {METADATA_URL: httpx.Response(503), TAGS_URL: _tags("llama3.2:3b")},
    )

    assert response.json()["hapi"] == "down"


def test_ollama_down_marks_both_ollama_fields_down():
    response = _get_health({METADATA_URL: _ok_metadata(), TAGS_URL: DOWN})

    assert response.status_code == 200
    assert response.json() == {"hapi": "ok", "ollama_http": "down", "ollama_model": "down"}


def test_ollama_error_status_marks_both_ollama_fields_down():
    # A valid, model-listing body: only the 500 status can make this "down".
    error_with_valid_body = httpx.Response(500, json={"models": [{"name": "llama3.2:3b"}]})
    response = _get_health({METADATA_URL: _ok_metadata(), TAGS_URL: error_with_valid_body})

    assert response.status_code == 200
    assert response.json() == {"hapi": "ok", "ollama_http": "down", "ollama_model": "down"}


def test_ollama_unparseable_body_marks_both_ollama_fields_down():
    response = _get_health(
        {METADATA_URL: _ok_metadata(), TAGS_URL: httpx.Response(200, text="not json")}
    )

    assert response.status_code == 200
    assert response.json() == {"hapi": "ok", "ollama_http": "down", "ollama_model": "down"}


def test_untagged_model_name_matches_latest_tag():
    response = _get_health(
        {METADATA_URL: _ok_metadata(), TAGS_URL: _tags("llama3.2:latest")},
        ollama_model="llama3.2",
    )

    assert response.json()["ollama_model"] == "ok"
