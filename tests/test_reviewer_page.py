# ORIGIN: AI — test cases typed by Claude Code from the agreed page rules (facts labeled as coming
#   from the FHIR record, the summary labeled as model prose, no decision offered, a patient id
#   never placed where a server log can see it), reviewed by Kiel.
"""GET /: the thin reviewer page.

The page is one HTML file whose small script calls the packet endpoint. These tests check what can
be checked without a browser: what is served, how it is labeled, what it must never contain, and
that it cannot be turned against the person reading it. How it looks and behaves in a browser is
checked by loading it in one.
"""

import re
from importlib.resources import files

import pytest
from fastapi.testclient import TestClient

from clinical_context.config import Settings, get_settings
from clinical_context.main import app

FHIR_PUBLIC = "http://hapi.example/fhir"


@pytest.fixture
def get_page():
    def _get(public_fhir_base_url: str = FHIR_PUBLIC):
        settings = Settings(_env_file=None, public_fhir_base_url=public_fhir_base_url)
        app.dependency_overrides[get_settings] = lambda: settings
        try:
            return TestClient(app).get("/")
        finally:
            app.dependency_overrides.clear()

    return _get


def test_the_root_serves_an_html_page_with_a_patient_id_box(get_page):
    response = get_page()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert re.search(r"<input[^>]+", response.text)
    assert 'id="patient-id"' in response.text
    assert "<form" in response.text and "<button" in response.text


def test_the_page_says_what_each_part_is_made_of(get_page):
    page = get_page().text.lower()

    # A reviewer must be able to tell a record from a model's sentence at a glance.
    assert "fhir record" in page
    assert "language model" in page
    assert "not from the record" in page
    # Both places that build a card of record facts (the patient, and the three lists) say so.
    assert get_page().text.count('"FHIR record"') >= 2


def test_the_page_offers_no_decision_and_uses_no_decision_language(get_page):
    page = get_page().text.lower()

    for word in (
        "approve",
        "deny",
        "denied",
        "authoriz",
        "determination",
        "eligib",
        "medically necessary",
        "confidence",
    ):
        assert word not in page, word


def test_the_source_links_point_at_the_configured_public_fhir_address(get_page):
    page = get_page("http://hapi.example/fhir/").text  # a trailing slash makes no difference

    assert 'data-fhir-base="http://hapi.example/fhir"' in page


def test_a_hostile_configured_address_cannot_break_out_of_the_page(get_page):
    page = get_page('http://x/"><script>alert(1)</script>').text

    assert "<script>alert(1)</script>" not in page
    assert "&quot;&gt;&lt;script&gt;" in page


def test_record_text_is_never_put_into_the_page_as_html(get_page):
    # Display names come from the FHIR record, which the page does not control. Everything from
    # a packet must go in as text, never as markup.
    page = get_page().text

    for dangerous in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert dangerous not in page, dangerous


def test_a_source_becomes_a_link_only_if_it_looks_like_a_resource_reference(get_page):
    # A source that is not "Type/id" must not become an href (for example "javascript:...").
    page = get_page().text

    assert re.search(r"SOURCE_PATTERN\s*=\s*/\^\[A-Za-z\]+", page)
    # ...and the guard is actually applied before any link is built, not just defined.
    assert re.search(r'if \(!SOURCE_PATTERN\.test\(source\)\) return el\("span"', page)


def test_the_patient_id_stays_out_of_the_query_string(get_page):
    # The server's access log prints query strings. A URL fragment is never sent to the server.
    page = get_page().text

    assert "location.hash" in page
    assert "location.search" not in page
    assert "URLSearchParams" not in page


def test_the_page_calls_the_packet_endpoint(get_page):
    page = get_page().text

    assert "/v1/patients/" in page and "/clinical-context" in page


def test_the_page_is_not_listed_in_the_api_docs():
    paths = TestClient(app).get("/openapi.json").json()["paths"]

    assert "/" not in paths
    assert "/v1/patients/{patient_id}/clinical-context" in paths


def test_the_page_is_served_with_a_policy_that_lets_it_talk_only_to_its_own_service(get_page):
    headers = get_page().headers

    policy = headers["content-security-policy"]
    assert "default-src 'none'" in policy
    assert "connect-src 'self'" in policy
    assert "base-uri 'none'" in policy
    assert "form-action 'none'" in policy
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"


def test_the_page_ships_inside_the_package_so_the_docker_image_has_it():
    assert (files("clinical_context") / "web" / "reviewer.html").is_file()


def test_every_link_out_of_the_page_opens_in_a_new_tab_without_passing_anything_along(get_page):
    page = get_page().text

    assert '"_blank"' in page
    assert '"noopener noreferrer"' in page
