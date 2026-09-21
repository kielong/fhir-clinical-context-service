# ORIGIN: AI — test cases typed by Claude Code from the agreed lookup and search rules, reviewed
#   by Kiel.
"""FhirClient: how a patient is found, and how every page of every list is fetched.

HAPI is faked with respx (see conftest.py). Nothing here touches the network.
"""

from contextlib import asynccontextmanager

import httpx
import pytest

import synthetic as syn
from clinical_context.config import Settings
from clinical_context.fhir.client import (
    AmbiguousPatient,
    FhirClient,
    FhirUnavailable,
    PatientNotFound,
)
from fhir_mocks import BASE, bundle, entry, operation_outcome

pytestmark = pytest.mark.anyio

SYNTHEA_UUID = "2fa15bc7-8866-461a-9000-f739e425860a"


def _settings(**overrides) -> Settings:
    defaults = {
        "fhir_base_url": BASE,
        "fhir_page_size": 100,
        "fhir_max_pages": 5,
        "fhir_timeout_seconds": 5,
    }
    return Settings(_env_file=None, **{**defaults, **overrides})


@asynccontextmanager
async def make_client(**overrides):
    async with httpx.AsyncClient() as http:
        yield FhirClient(http, _settings(**overrides))


# ============================================================ resolving a patient


async def test_a_hapi_id_is_fetched_directly_with_no_identifier_search(hapi):
    hapi.get(f"{BASE}/Patient/1000").respond(200, json=syn.patient("1000"))

    async with make_client() as fhir:
        patient = await fhir.resolve_patient("1000")

    assert patient["id"] == "1000"
    assert [call.request.url.path for call in hapi.calls] == ["/fhir/Patient/1000"]


@pytest.mark.parametrize("status", [400, 404, 410])
async def test_when_the_id_is_unknown_it_is_tried_as_an_identifier(hapi, status):
    # The assignment's example id is a Synthea identifier, not a HAPI id, so this path is the
    # normal one for it.
    hapi.get(f"{BASE}/Patient/{SYNTHEA_UUID}").respond(status, json=operation_outcome())
    search = hapi.get(f"{BASE}/Patient", params={"identifier": SYNTHEA_UUID}).respond(
        200, json=bundle([syn.patient("1000")])
    )

    async with make_client() as fhir:
        patient = await fhir.resolve_patient(SYNTHEA_UUID)

    assert patient["id"] == "1000"
    # System-less on purpose: the Synthea UUID sits under two systems on ONE patient, and
    # _count=2 is enough to tell "one match" from "several".
    assert dict(search.calls.last.request.url.params) == {"identifier": SYNTHEA_UUID, "_count": "2"}


async def test_no_match_by_id_or_identifier_is_patient_not_found(hapi):
    hapi.get(f"{BASE}/Patient/nobody").respond(404, json=operation_outcome())
    hapi.get(f"{BASE}/Patient", params={"identifier": "nobody"}).respond(200, json=bundle([]))

    async with make_client() as fhir:
        with pytest.raises(PatientNotFound):
            await fhir.resolve_patient("nobody")


async def test_two_patients_with_one_identifier_is_ambiguous(hapi):
    # This means duplicates from a bad load; showing one of them arbitrarily would be worse.
    hapi.get(f"{BASE}/Patient/dup").respond(404, json=operation_outcome())
    hapi.get(f"{BASE}/Patient", params={"identifier": "dup"}).respond(
        200, json=bundle([syn.patient("1"), syn.patient("2")])
    )

    async with make_client() as fhir:
        with pytest.raises(AmbiguousPatient):
            await fhir.resolve_patient("dup")


async def test_included_and_outcome_entries_do_not_count_as_matches(hapi):
    hapi.get(f"{BASE}/Patient/x1").respond(404, json=operation_outcome())
    found = bundle(
        [],
        entries=[
            entry(syn.patient("1000")),
            entry({"resourceType": "Organization", "id": "9"}, mode="include"),
            entry(operation_outcome(), mode="outcome"),
        ],
    )
    hapi.get(f"{BASE}/Patient", params={"identifier": "x1"}).respond(200, json=found)

    async with make_client() as fhir:
        assert (await fhir.resolve_patient("x1"))["id"] == "1000"


async def test_the_id_is_url_encoded_into_the_path(hapi):
    route = hapi.route(method="GET", path__startswith="/fhir/Patient/").respond(
        404, json=operation_outcome()
    )
    hapi.get(f"{BASE}/Patient").respond(200, json=bundle([]))

    async with make_client() as fhir:
        with pytest.raises(PatientNotFound):
            await fhir.resolve_patient("a/b?c")

    assert route.calls.last.request.url.raw_path == b"/fhir/Patient/a%2Fb%3Fc"


async def test_every_request_bypasses_the_search_cache_and_asks_for_fhir_json(hapi):
    # HAPI reuses identical searches for 60 s; without this, a patient loaded a moment ago can
    # look empty.
    hapi.get(f"{BASE}/Patient/1000").respond(200, json=syn.patient("1000"))

    async with make_client() as fhir:
        await fhir.resolve_patient("1000")

    headers = hapi.calls.last.request.headers
    assert headers["Cache-Control"] == "no-cache"
    assert headers["Accept"] == "application/fhir+json"


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("bad")],
)
async def test_a_network_failure_is_fhir_unavailable(hapi, failure):
    hapi.get(f"{BASE}/Patient/1000").mock(side_effect=failure)

    async with make_client() as fhir:
        with pytest.raises(FhirUnavailable):
            await fhir.resolve_patient("1000")


@pytest.mark.parametrize("status", [401, 403, 500, 503])
async def test_an_unexpected_status_by_id_is_fhir_unavailable_not_not_found(hapi, status):
    # 404 means "no such patient". A 500 or a 403 means we do not know, so it must not be
    # reported as "no such patient".
    hapi.get(f"{BASE}/Patient/1000").respond(status, json=operation_outcome("boom"))

    async with make_client() as fhir:
        with pytest.raises(FhirUnavailable):
            await fhir.resolve_patient("1000")


async def test_an_error_on_the_identifier_search_is_fhir_unavailable(hapi):
    hapi.get(f"{BASE}/Patient/x").respond(404, json=operation_outcome())
    hapi.get(f"{BASE}/Patient", params={"identifier": "x"}).respond(500, json=operation_outcome())

    async with make_client() as fhir:
        with pytest.raises(FhirUnavailable):
            await fhir.resolve_patient("x")


@pytest.mark.parametrize(
    "body",
    [
        httpx.Response(200, text="<html>gateway page</html>"),  # not JSON
        httpx.Response(200, json=operation_outcome()),  # JSON, but not a Patient
        httpx.Response(200, json=syn.patient(None)),  # a Patient with no id
    ],
)
async def test_a_200_that_is_not_a_usable_patient_is_fhir_unavailable(hapi, body):
    hapi.get(f"{BASE}/Patient/1000").mock(return_value=body)

    async with make_client() as fhir:
        with pytest.raises(FhirUnavailable):
            await fhir.resolve_patient("1000")


# ============================================================ searching with pagination


async def test_a_search_sends_only_the_patient_and_the_page_size(hapi):
    route = hapi.get(f"{BASE}/Condition").respond(200, json=bundle([syn.condition("c1")]))

    async with make_client(fhir_page_size=50) as fhir:
        await fhir.search_by_patient("Condition", "1000")

    request = route.calls.last.request
    # No status filter: filtering happens in assembly, which is what lets it count what it left out.
    assert dict(request.url.params) == {"patient": "1000", "_count": "50"}
    assert request.headers["Cache-Control"] == "no-cache"
    assert "$everything" not in str(request.url)


async def test_every_page_is_fetched_and_the_client_returns_the_union(hapi):
    hapi.get(f"{BASE}", params={"_getpages": "abc", "_getpagesoffset": "2"}).respond(
        200, json=bundle([syn.condition("c3")])
    )
    hapi.get(f"{BASE}/Condition").respond(
        200,
        json=bundle(
            [syn.condition("c1"), syn.condition("c2")],
            next_url=f"{BASE}?_getpages=abc&_getpagesoffset=2",
        ),
    )

    async with make_client() as fhir:
        resources = await fhir.search_by_patient("Condition", "1000")

    # More than the page size, and more than a cap would keep: the client never stops early.
    assert [r["id"] for r in resources] == ["c1", "c2", "c3"]


async def test_thirteen_pages_are_all_fetched_like_the_worst_real_patient(hapi):
    # Floyd420 has 1,275 medication requests: 13 pages at 100 per page.
    total, page_size = 1275, 100

    def answer(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("_getpagesoffset", 0))
        ids = range(offset, min(offset + page_size, total))
        more = offset + page_size < total
        next_url = f"{BASE}?_getpages=abc&_getpagesoffset={offset + page_size}" if more else None
        return httpx.Response(
            200, json=bundle([syn.medication(f"m{i}") for i in ids], next_url=next_url)
        )

    hapi.get(f"{BASE}", params={"_getpages": "abc"}).mock(side_effect=answer)
    hapi.get(f"{BASE}/MedicationRequest").mock(side_effect=answer)

    async with make_client(fhir_max_pages=50) as fhir:
        resources = await fhir.search_by_patient("MedicationRequest", "1319")

    assert len(resources) == 1275
    assert len({r["id"] for r in resources}) == 1275  # no page repeated, none skipped


async def test_a_next_link_is_re_based_onto_the_configured_server(hapi):
    # HAPI builds paging links from its own idea of its address; from another container that
    # address (localhost) is unreachable, so only the path and query of the link are trusted.
    hapi.get(f"{BASE}", params={"_getpages": "abc"}).respond(
        200, json=bundle([syn.condition("c2")])
    )
    hapi.get(f"{BASE}/Condition").respond(
        200,
        json=bundle([syn.condition("c1")], next_url="http://localhost:8080/fhir?_getpages=abc"),
    )

    async with make_client() as fhir:
        resources = await fhir.search_by_patient("Condition", "1000")

    assert [r["id"] for r in resources] == ["c1", "c2"]
    assert {call.request.url.host for call in hapi.calls} == {"hapi.test"}


async def test_included_outcome_and_other_type_entries_are_not_results(hapi):
    mixed = bundle(
        [],
        entries=[
            entry(syn.condition("c1")),
            entry({"resourceType": "Encounter", "id": "e1"}, mode="include"),
            entry({"resourceType": "Encounter", "id": "e2"}),  # wrong type, even if "match"
            entry(operation_outcome(), mode="outcome"),
        ],
    )
    hapi.get(f"{BASE}/Condition").respond(200, json=mixed)

    async with make_client() as fhir:
        resources = await fhir.search_by_patient("Condition", "1000")

    assert [r["id"] for r in resources] == ["c1"]


async def test_an_entry_of_the_right_type_that_was_only_included_is_not_a_result(hapi):
    # A Condition can appear in a bundle only because it was `_include`d by another result. It is
    # the right type but not a match for this search, so it must not be counted.
    with_include = bundle(
        [], entries=[entry(syn.condition("c1")), entry(syn.condition("c2"), mode="include")]
    )
    hapi.get(f"{BASE}/Condition").respond(200, json=with_include)

    async with make_client() as fhir:
        resources = await fhir.search_by_patient("Condition", "1000")

    assert [r["id"] for r in resources] == ["c1"]


async def test_an_empty_result_is_an_empty_list(hapi):
    hapi.get(f"{BASE}/AllergyIntolerance").respond(200, json=bundle([]))

    async with make_client() as fhir:
        assert await fhir.search_by_patient("AllergyIntolerance", "1000") == []


async def test_a_search_that_never_ends_hits_the_page_limit_and_fails_loudly(hapi):
    loop = bundle([syn.condition("c1")], next_url=f"{BASE}?_getpages=abc")
    hapi.get(f"{BASE}", params={"_getpages": "abc"}).respond(200, json=loop)
    hapi.get(f"{BASE}/Condition").respond(200, json=loop)

    async with make_client(fhir_max_pages=3) as fhir:
        with pytest.raises(FhirUnavailable, match="3 pages"):
            await fhir.search_by_patient("Condition", "1000")

    assert len(hapi.calls) == 3  # it stopped at the limit; it did not loop


async def test_exactly_the_page_limit_is_fine_when_there_is_no_next_link(hapi):
    hapi.get(f"{BASE}", params={"_getpages": "abc"}).respond(
        200, json=bundle([syn.condition("c2")])
    )
    hapi.get(f"{BASE}/Condition").respond(
        200, json=bundle([syn.condition("c1")], next_url=f"{BASE}?_getpages=abc")
    )

    async with make_client(fhir_max_pages=2) as fhir:
        resources = await fhir.search_by_patient("Condition", "1000")

    assert len(resources) == 2


async def test_a_failure_on_a_later_page_fails_the_whole_search(hapi):
    # Half a list would look like a complete one; better to fail than to mislead.
    hapi.get(f"{BASE}", params={"_getpages": "abc"}).respond(500, json=operation_outcome())
    hapi.get(f"{BASE}/Condition").respond(
        200, json=bundle([syn.condition("c1")], next_url=f"{BASE}?_getpages=abc")
    )

    async with make_client() as fhir:
        with pytest.raises(FhirUnavailable):
            await fhir.search_by_patient("Condition", "1000")


async def test_a_search_timeout_is_fhir_unavailable(hapi):
    hapi.get(f"{BASE}/Condition").mock(side_effect=httpx.ReadTimeout("slow"))

    async with make_client() as fhir:
        with pytest.raises(FhirUnavailable):
            await fhir.search_by_patient("Condition", "1000")


# ============================================================ ping


async def test_ping_is_true_only_for_a_200_from_metadata(hapi):
    route = hapi.get(f"{BASE}/metadata").respond(200, json={"resourceType": "CapabilityStatement"})

    async with make_client() as fhir:
        assert await fhir.ping() is True

    assert route.called


@pytest.mark.parametrize(
    "outcome", [httpx.Response(503), httpx.ConnectError("refused"), httpx.ReadTimeout("slow")]
)
async def test_ping_is_false_for_an_error_status_or_no_answer(hapi, outcome):
    route = hapi.get(f"{BASE}/metadata")
    if isinstance(outcome, httpx.Response):
        route.mock(return_value=outcome)
    else:
        route.mock(side_effect=outcome)

    async with make_client() as fhir:
        assert await fhir.ping() is False
