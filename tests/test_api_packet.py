# ORIGIN: AI — test cases typed by Claude Code from the agreed error contract, reviewed by Kiel.
"""GET /v1/patients/{patient_id}/clinical-context, end to end with a fake HAPI.

The whole request path runs for real (validation, lookup, paging, assembly, error mapping,
logging); only HAPI is faked.
"""

import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

import synthetic as syn
from clinical_context.config import Settings, get_settings
from clinical_context.dependencies import get_fhir_client, get_summarizer
from clinical_context.main import app
from clinical_context.models import ClinicalContextPacket, SummaryBlock
from clinical_context.privacy import patient_hash
from clinical_context.summarizer import SummaryResult
from fhir_mocks import BASE, bundle, operation_outcome
from ollama_mocks import CHAT, GOOD, OLLAMA, reply

FIXTURES = Path(__file__).parent / "fixtures" / "real"
SYNTHEA_UUID = "2fa15bc7-8866-461a-9000-f739e425860a"  # the assignment's example patient
PACKET = "/v1/patients/{}/clinical-context"


def _settings(**overrides) -> Settings:
    defaults = {
        "fhir_base_url": BASE,
        "fhir_page_size": 100,
        "fhir_max_pages": 5,
        "fhir_timeout_seconds": 5,
        "as_of_date": date(2019, 9, 16),
        "ollama_host": OLLAMA,
        "ollama_model": "llama3.2:3b",
        "ollama_timeout_seconds": 5,
        "ollama_warmup": False,
    }
    return Settings(_env_file=None, **{**defaults, **overrides})


async def _no_model(packet):
    """A summarizer that never calls a model, so a test can be about the endpoint alone."""
    block = SummaryBlock(text=None, status="unavailable", model=None, reason=None)
    return SummaryResult(block=block, timings=None, elapsed_ms=None, attempts=0)


@pytest.fixture
def api(hapi):
    """A factory for a running app with a fake HAPI behind it: `client = api(as_of_date=None)`.

    The model is skipped unless the test asks for it with `api(model=True)` (and then fakes Ollama).
    """
    started: list[TestClient] = []

    def build(*, model: bool = False, **settings_overrides) -> TestClient:
        settings = _settings(**settings_overrides)
        app.dependency_overrides[get_settings] = lambda: settings
        if model:
            app.dependency_overrides.pop(get_summarizer, None)  # use the real summarizer
        else:
            app.dependency_overrides[get_summarizer] = lambda: _no_model
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()  # runs the app lifespan, which creates the shared HTTP client
        started.append(client)
        return client

    yield build
    for client in started:
        client.__exit__(None, None, None)
    app.dependency_overrides.clear()


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _mount_lists(hapi, conditions=(), medications=(), allergies=()):
    hapi.get(f"{BASE}/Condition").respond(200, json=bundle(list(conditions)))
    hapi.get(f"{BASE}/MedicationRequest").respond(200, json=bundle(list(medications)))
    hapi.get(f"{BASE}/AllergyIntolerance").respond(200, json=bundle(list(allergies)))


def _mount_aaron_by_identifier(hapi) -> dict:
    """The assignment's patient, reachable only through the identifier fallback (as in HAPI)."""
    fixture = _fixture("Aaron697_Brekke496")
    patient = {**fixture["patient"], "id": "1000"}  # HAPI assigns its own id
    hapi.get(f"{BASE}/Patient/{SYNTHEA_UUID}").respond(404, json=operation_outcome())
    hapi.get(f"{BASE}/Patient", params={"identifier": SYNTHEA_UUID}).respond(
        200, json=bundle([patient])
    )
    _mount_lists(hapi, fixture["conditions"], fixture["medications"], fixture["allergies"])
    return patient


# ============================================================ the success path


def test_the_assignment_patient_resolves_by_identifier_and_returns_a_packet(hapi, api):
    _mount_aaron_by_identifier(hapi)

    response = api().get(PACKET.format(SYNTHEA_UUID))

    assert response.status_code == 200
    packet = ClinicalContextPacket.model_validate(response.json())  # matches the schema
    assert packet.patient_id == SYNTHEA_UUID  # the caller's id is echoed back
    assert packet.patient.fhir_id == "1000"  # ...and the server's id is reported separately
    assert packet.patient.source == "Patient/1000"
    assert packet.patient.age_years == 73
    assert packet.meta.as_of == date(2019, 9, 16)
    assert packet.meta.as_of_source == "config"
    assert len(packet.conditions) == 5
    assert all(c.source.startswith("Condition/") for c in packet.conditions)
    assert packet.medications == []
    meds_gap = next(m for m in packet.missing if m.section == "medications")
    assert "1 other on file was excluded" in meds_gap.detail


def test_a_hapi_id_returns_the_same_patient(hapi, api):
    patient = _mount_aaron_by_identifier(hapi)
    hapi.get(f"{BASE}/Patient/1000").respond(200, json=patient)

    response = api().get(PACKET.format("1000"))

    assert response.status_code == 200
    assert response.json()["patient"]["fhir_id"] == "1000"
    assert response.json()["patient_id"] == "1000"


def test_the_packet_is_a_success_even_though_there_is_no_model_yet(hapi, api):
    _mount_aaron_by_identifier(hapi)

    body = api().get(PACKET.format(SYNTHEA_UUID)).json()

    # No Ollama: the facts are the product, and the summary is honestly unavailable.
    assert body["summary"] == {"text": None, "status": "unavailable", "model": None, "reason": None}
    assert body["conditions"] and body["missing"] and body["meta"]


def test_the_summary_comes_from_the_summarizer_and_never_changes_the_facts(hapi, api):
    _mount_aaron_by_identifier(hapi)
    client = api()
    baseline = client.get(PACKET.format(SYNTHEA_UUID)).json()

    async def fake(packet):
        block = SummaryBlock(
            text="Two plain sentences.", status="generated", model="m", reason=None
        )
        return SummaryResult(block=block, timings=None, elapsed_ms=7, attempts=1)

    app.dependency_overrides[get_summarizer] = lambda: fake
    body = client.get(PACKET.format(SYNTHEA_UUID)).json()

    assert body["summary"]["text"] == "Two plain sentences."
    assert {k: v for k, v in body.items() if k not in ("summary", "meta")} == {
        k: v for k, v in baseline.items() if k not in ("summary", "meta")
    }


def test_the_reference_date_is_today_when_none_is_configured(hapi, api):
    _mount_aaron_by_identifier(hapi)

    meta = api(as_of_date=None).get(PACKET.format(SYNTHEA_UUID)).json()["meta"]

    assert meta["as_of_source"] == "now"
    assert meta["as_of"] == datetime.now(UTC).date().isoformat()


def test_timings_report_fhir_and_total_and_leave_the_model_null(hapi, api):
    _mount_aaron_by_identifier(hapi)

    timings = api().get(PACKET.format(SYNTHEA_UUID)).json()["meta"]["timings_ms"]

    assert timings["llm"] is None
    assert isinstance(timings["fhir"], int) and isinstance(timings["total"], int)
    assert 0 <= timings["fhir"] <= timings["total"]


def test_a_long_medication_history_is_fully_fetched_then_filtered_and_counted(hapi, api):
    # 250 medication requests across 3 pages: 10 active, 240 stopped. All 250 must be fetched so
    # the packet can say how many it left out.
    meds = [syn.medication(f"a{i}", status="active") for i in range(10)]
    meds += [syn.medication(f"s{i}", status="stopped") for i in range(240)]
    patient = syn.patient("1000")
    hapi.get(f"{BASE}/Patient/1000").respond(200, json=patient)
    _mount_lists(hapi)

    def page(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("_getpagesoffset", 0))
        chunk = meds[offset : offset + 100]
        more = offset + 100 < len(meds)
        nxt = f"{BASE}?_getpages=m&_getpagesoffset={offset + 100}" if more else None
        return httpx.Response(200, json=bundle(chunk, next_url=nxt))

    hapi.get(f"{BASE}", params={"_getpages": "m"}).mock(side_effect=page)
    hapi.get(f"{BASE}/MedicationRequest").mock(side_effect=page)

    body = api().get(PACKET.format("1000")).json()

    assert len(body["medications"]) == 10
    assert body["meta"]["excluded_counts"]["medications"] == 240


def test_the_three_lists_are_searched_by_patient_with_no_filter_and_never_everything(hapi, api):
    _mount_aaron_by_identifier(hapi)

    api().get(PACKET.format(SYNTHEA_UUID))

    list_calls = [
        c.request for c in hapi.calls if not c.request.url.path.startswith("/fhir/Patient")
    ]
    assert {r.url.path for r in list_calls} == {
        "/fhir/Condition",
        "/fhir/MedicationRequest",
        "/fhir/AllergyIntolerance",
    }
    assert all(dict(r.url.params) == {"patient": "1000", "_count": "100"} for r in list_calls)
    assert not any("$everything" in str(c.request.url) for c in hapi.calls)


# ============================================================ with the model (Ollama faked)


def _facts(body: dict) -> dict:
    return {k: v for k, v in body.items() if k not in ("summary", "meta")}


def test_a_generated_summary_is_added_and_the_facts_are_unchanged(hapi, api):
    _mount_aaron_by_identifier(hapi)
    baseline = api().get(PACKET.format(SYNTHEA_UUID)).json()
    hapi.post(CHAT).respond(200, json=reply())

    body = api(model=True).get(PACKET.format(SYNTHEA_UUID)).json()

    assert body["summary"] == {
        "text": GOOD,
        "status": "generated",
        "model": "llama3.2:3b",
        "reason": None,
    }
    assert _facts(body) == _facts(baseline)  # the model only ever adds the summary
    assert isinstance(body["meta"]["timings_ms"]["llm"], int)


def test_when_ollama_is_down_the_facts_still_come_back_with_an_unavailable_summary(hapi, api):
    _mount_aaron_by_identifier(hapi)
    baseline = api().get(PACKET.format(SYNTHEA_UUID)).json()
    hapi.post(CHAT).mock(side_effect=httpx.ConnectError("refused"))

    response = api(model=True).get(PACKET.format(SYNTHEA_UUID))

    assert response.status_code == 200  # failing closed on prose, not on evidence
    body = response.json()
    assert body["summary"] == {
        "text": None,
        "status": "unavailable",
        "model": "llama3.2:3b",
        "reason": "model_unreachable",
    }
    assert _facts(body) == _facts(baseline)


def test_a_model_that_breaks_the_rules_never_reaches_the_reviewer(hapi, api):
    _mount_aaron_by_identifier(hapi)
    hapi.post(CHAT).respond(200, json=reply("Approval is recommended. Coverage is warranted."))

    response = api(model=True).get(PACKET.format(SYNTHEA_UUID))

    assert response.status_code == 200
    assert response.json()["summary"]["reason"] == "policy_violation"
    assert "Approval" not in response.text and "recommended" not in response.text


def test_asking_again_gives_the_same_words_even_if_the_model_would_say_it_differently(hapi, api):
    _mount_aaron_by_identifier(hapi)
    route = hapi.post(CHAT)
    route.side_effect = [
        httpx.Response(200, json=reply("First wording of the summary.")),
        httpx.Response(200, json=reply("A different wording of the summary.")),
    ]
    client = api(model=True)

    first = client.get(PACKET.format(SYNTHEA_UUID)).json()
    second = client.get(PACKET.format(SYNTHEA_UUID)).json()

    assert first["summary"]["text"] == second["summary"]["text"] == "First wording of the summary."
    assert route.call_count == 1


def test_a_summarizer_that_crashes_still_returns_the_facts(hapi, api, caplog):
    _mount_aaron_by_identifier(hapi)
    caplog.set_level(logging.INFO, logger="clinical_context")
    client = api()
    baseline = client.get(PACKET.format(SYNTHEA_UUID)).json()

    async def explode(packet):
        raise RuntimeError("Aaron697 Brekke496 has Prediabetes")

    app.dependency_overrides[get_summarizer] = lambda: explode
    response = client.get(PACKET.format(SYNTHEA_UUID))

    assert response.status_code == 200  # a bug in the writer must never cost the reviewer the facts
    body = response.json()
    assert body["summary"]["status"] == "unavailable"
    assert body["summary"]["reason"] == "internal_error"
    assert _facts(body) == _facts(baseline)
    logged = " ".join(
        r.getMessage() for r in caplog.records if r.name.startswith("clinical_context")
    )
    assert "RuntimeError" in logged and "Aaron" not in logged and "Prediabetes" not in logged


# ============================================================ the error contract


def test_an_unknown_patient_is_404(hapi, api):
    hapi.get(f"{BASE}/Patient/nobody").respond(404, json=operation_outcome())
    hapi.get(f"{BASE}/Patient", params={"identifier": "nobody"}).respond(200, json=bundle([]))

    response = api().get(PACKET.format("nobody"))

    assert response.status_code == 404
    assert response.json() == {"detail": "Patient not found"}


def test_an_identifier_shared_by_two_patients_is_409(hapi, api):
    hapi.get(f"{BASE}/Patient/dup").respond(404, json=operation_outcome())
    hapi.get(f"{BASE}/Patient", params={"identifier": "dup"}).respond(
        200, json=bundle([syn.patient("1"), syn.patient("2")])
    )

    response = api().get(PACKET.format("dup"))

    assert response.status_code == 409
    assert response.json() == {"detail": "More than one patient matches this identifier"}


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param({"side_effect": httpx.ConnectError("refused")}, id="hapi-down"),
        pytest.param({"side_effect": httpx.ReadTimeout("slow")}, id="hapi-timeout"),
        pytest.param({"return_value": httpx.Response(503)}, id="hapi-5xx"),
    ],
)
def test_hapi_failing_is_502(hapi, api, failure):
    hapi.get(f"{BASE}/Patient/1000").mock(**failure)

    response = api().get(PACKET.format("1000"))

    assert response.status_code == 502
    assert response.json() == {"detail": "The FHIR server is unavailable"}


def test_one_failing_list_fails_the_packet_even_if_the_others_worked(hapi, api):
    # A packet missing its medications would read as "no medications". Better to fail.
    hapi.get(f"{BASE}/Patient/1000").respond(200, json=syn.patient("1000"))
    hapi.get(f"{BASE}/Condition").respond(200, json=bundle([syn.condition("c1")]))
    hapi.get(f"{BASE}/MedicationRequest").respond(500, json=operation_outcome())
    hapi.get(f"{BASE}/AllergyIntolerance").respond(200, json=bundle([]))

    assert api().get(PACKET.format("1000")).status_code == 502


def test_hitting_the_page_limit_is_502(hapi, api):
    hapi.get(f"{BASE}/Patient/1000").respond(200, json=syn.patient("1000"))
    endless = bundle([syn.condition("c1")], next_url=f"{BASE}?_getpages=x")
    hapi.get(f"{BASE}", params={"_getpages": "x"}).respond(200, json=endless)
    hapi.get(f"{BASE}/Condition").respond(200, json=endless)
    hapi.get(f"{BASE}/MedicationRequest").respond(200, json=bundle([]))
    hapi.get(f"{BASE}/AllergyIntolerance").respond(200, json=bundle([]))

    assert api(fhir_max_pages=2).get(PACKET.format("1000")).status_code == 502


@pytest.mark.parametrize("bad_id", ["a b", "x|y", "a?b", "a#b", "x" * 65, "café", "a;b", "abc\n"])
def test_a_malformed_id_is_422_and_never_reaches_hapi(hapi, api, bad_id):
    response = api().get(PACKET.format(quote(bad_id)))

    assert response.status_code == 422
    assert response.json() == {
        "detail": "patient_id must be 1-64 characters: letters, digits, '.' or '-'"
    }
    assert hapi.calls.call_count == 0
    assert bad_id not in response.text  # never reflect what the caller typed (it could be an SSN)


@pytest.mark.parametrize(
    "path", ["/v1/patients//clinical-context", "/v1/patients/a%2Fb/clinical-context"]
)
def test_an_empty_or_slashed_id_never_reaches_hapi(hapi, api, path):
    response = api().get(path)

    assert response.status_code in (404, 422)
    assert hapi.calls.call_count == 0


@pytest.mark.parametrize("good_id", ["a.b-c", "x" * 64, "1000", SYNTHEA_UUID.upper()])
def test_ids_at_the_edge_of_the_rule_are_accepted_and_then_looked_up(hapi, api, good_id):
    hapi.get(f"{BASE}/Patient/{good_id}").respond(404, json=operation_outcome())
    hapi.get(f"{BASE}/Patient", params={"identifier": good_id}).respond(200, json=bundle([]))

    assert api().get(PACKET.format(good_id)).status_code == 404  # not 422: it got to HAPI


def test_an_unexpected_crash_is_a_generic_500_with_no_stack_and_no_data(hapi, api, caplog):
    _mount_aaron_by_identifier(hapi)
    caplog.set_level(logging.INFO, logger="clinical_context")
    client = api()

    class Exploding:
        async def resolve_patient(self, patient_id):
            raise RuntimeError("Aaron697 Brekke496 has Prediabetes 2fa15bc7")

    app.dependency_overrides[get_fhir_client] = lambda: Exploding()
    response = client.get(PACKET.format(SYNTHEA_UUID))

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    for leaked in ("Traceback", "RuntimeError", "Aaron", "Prediabetes", "2fa15bc7"):
        assert leaked not in response.text

    # The server log says what kind of error and where, but never the message: a message can
    # contain patient data.
    logged = " ".join(
        r.getMessage() for r in caplog.records if r.name.startswith("clinical_context")
    )
    assert "unexpected RuntimeError" in logged
    for private in ("Aaron", "Prediabetes", "2fa15bc7"):
        assert private not in logged


# ============================================================ logging and the contract


def test_the_log_line_has_counts_and_a_hash_but_no_names_facts_or_ids(hapi, api, caplog):
    _mount_aaron_by_identifier(hapi)
    caplog.set_level(logging.INFO, logger="clinical_context")

    api().get(PACKET.format(SYNTHEA_UUID))

    lines = [r.getMessage() for r in caplog.records if r.name == "clinical_context.request"]
    assert len(lines) == 1
    line = lines[0]
    assert patient_hash(SYNTHEA_UUID) in line
    assert "conditions=5" in line and "medications=0" in line and "truncated=False" in line
    assert "summary=unavailable" in line
    for private in ("Aaron", "Brekke", "Prediabetes", "Ibuprofen", SYNTHEA_UUID, "1000"):
        assert private not in line


def test_a_failed_request_logs_only_a_hash_of_the_id(hapi, api, caplog):
    hapi.get(f"{BASE}/Patient/nobody").respond(404, json=operation_outcome())
    hapi.get(f"{BASE}/Patient", params={"identifier": "nobody"}).respond(200, json=bundle([]))
    caplog.set_level(logging.INFO, logger="clinical_context")

    api().get(PACKET.format("nobody"))

    text = " ".join(r.getMessage() for r in caplog.records if r.name.startswith("clinical_context"))
    assert patient_hash("nobody") in text
    assert "nobody" not in text


def test_the_openapi_contract_is_the_packet_model(api):
    schema = api().get("/openapi.json").json()
    ok = schema["paths"]["/v1/patients/{patient_id}/clinical-context"]["get"]["responses"]["200"]

    assert ok["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ClinicalContextPacket"
    }
    assert "approved" not in json.dumps(schema)  # nothing in the contract can decide anything
