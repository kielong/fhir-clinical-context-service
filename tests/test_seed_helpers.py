# ORIGIN: AI — test cases drafted and typed by Claude Code, reviewed by Kiel
"""The seed's pure helpers and its policy. No network: HTTP is an httpx.MockTransport."""

import json
from pathlib import Path

import httpx
import pytest

import seed_hapi
from synthea_data import (
    SYNTHEA_IDENTIFIER_SYSTEM,
    order_bundles,
    read_priority,
    uuid_from_filename,
)

UUID_A = "2fa15bc7-8866-461a-9000-f739e425860a"
UUID_B = "0979f4fe-08c5-414e-ba1c-6ccf852bcce4"
UUID_C = "9da0dcfc-05e3-4e8e-95ff-b04b56f748be"
UUID_D = "e2129449-9c68-4155-a826-e22091aa4742"
BASE_URL = "http://hapi.test/fhir"


def _bundle_path(name: str, uuid: str) -> Path:
    return Path("data/synthea/fhir") / f"{name}_{uuid}.json"


# ---------------------------------------------------------------- uuid_from_filename


def test_uuid_comes_from_the_end_of_the_filename():
    assert uuid_from_filename(_bundle_path("Aaron697_Brekke496", UUID_A)) == UUID_A


def test_uuid_from_filename_works_for_a_plain_string_name():
    assert uuid_from_filename(f"Aaron697_Brekke496_{UUID_A}.json") == UUID_A


def test_filename_without_a_uuid_is_rejected():
    with pytest.raises(ValueError, match="hospitalInformation1234.json"):
        uuid_from_filename(Path("hospitalInformation1234.json"))


# ---------------------------------------------------------------- read_priority


def test_priority_file_skips_blanks_and_comments_and_keeps_order(tmp_path):
    priority_file = tmp_path / "priority.txt"
    priority_file.write_text(
        f"# header comment\n\n{UUID_B}  # Floyd420: pagination stress\n{UUID_A}\n   \n"
    )

    assert read_priority(priority_file) == [UUID_B, UUID_A]


def test_priority_file_drops_duplicates_keeping_the_first(tmp_path):
    priority_file = tmp_path / "priority.txt"
    priority_file.write_text(f"{UUID_A}\n{UUID_B}\n{UUID_A}\n")

    assert read_priority(priority_file) == [UUID_A, UUID_B]


def test_priority_file_rejects_a_line_that_is_not_a_uuid(tmp_path):
    priority_file = tmp_path / "priority.txt"
    priority_file.write_text(f"{UUID_A}\nnot-a-uuid\n")

    with pytest.raises(ValueError, match="line 2"):
        read_priority(priority_file)


# ---------------------------------------------------------------- order_bundles


def _files() -> list[Path]:
    # Deliberately unsorted; alphabetical order by filename is Aaron, Alicia, Floyd, Zed.
    return [
        _bundle_path("Zed1_Last1", UUID_D),
        _bundle_path("Floyd420_Jerde200", UUID_B),
        _bundle_path("Aaron697_Brekke496", UUID_A),
        _bundle_path("Alicia629_Walter473", UUID_C),
    ]


def test_priority_bundles_come_first_in_priority_order_then_the_rest_alphabetically():
    ordered, missing = order_bundles(_files(), priority=[UUID_B, UUID_D], limit=None)

    assert [uuid_from_filename(p) for p in ordered] == [UUID_B, UUID_D, UUID_A, UUID_C]
    assert missing == []


def test_limit_counts_priority_bundles_first():
    ordered, _ = order_bundles(_files(), priority=[UUID_B, UUID_D, UUID_A], limit=2)

    assert [uuid_from_filename(p) for p in ordered] == [UUID_B, UUID_D]


def test_limit_larger_than_the_data_returns_everything():
    ordered, _ = order_bundles(_files(), priority=[UUID_B], limit=1000)

    assert len(ordered) == 4


def test_no_limit_returns_everything():
    ordered, _ = order_bundles(_files(), priority=[], limit=None)

    assert [p.name for p in ordered] == sorted(p.name for p in _files())


def test_priority_uuids_with_no_file_are_reported_not_ignored():
    unknown = "11111111-1111-4111-8111-111111111111"

    ordered, missing = order_bundles(_files(), priority=[unknown, UUID_A], limit=None)

    assert missing == [unknown]
    assert uuid_from_filename(ordered[0]) == UUID_A  # the found priority patient is still first


@pytest.mark.parametrize("bad_limit", [0, -1])
def test_a_limit_below_one_is_an_error_not_a_silent_no_op(bad_limit):
    with pytest.raises(ValueError, match="limit"):
        order_bundles(_files(), priority=[], limit=bad_limit)


# ---------------------------------------------------------------- seed policy (mocked HTTP)


class FakeHapi:
    """Records requests and answers probes and posts the way a test scripts them."""

    def __init__(self, *, probe_totals: list[int], post_response=None, post_error=None):
        self.probe_totals = list(probe_totals)  # one answer per probe, in order
        self.post_response = post_response or httpx.Response(200, json={"resourceType": "Bundle"})
        self.post_error = post_error
        self.probes: list[httpx.Request] = []
        self.posts: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            self.probes.append(request)
            total = self.probe_totals.pop(0)
            return httpx.Response(200, json={"resourceType": "Bundle", "total": total})
        self.posts.append(request)
        if self.post_error:
            raise self.post_error
        return self.post_response

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def _write_bundle(tmp_path: Path, entries: int = 3, uuid: str = UUID_A) -> Path:
    path = tmp_path / f"Test_Patient_{uuid}.json"
    bundle = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {"resource": {"resourceType": "Observation", "id": str(i)}} for i in range(entries)
        ],
    }
    path.write_text(json.dumps(bundle))
    return path


def test_probe_asks_for_the_synthea_identifier_and_bypasses_the_search_cache():
    hapi = FakeHapi(probe_totals=[0])

    exists = seed_hapi.probe_patient_exists(hapi.client(), BASE_URL, UUID_A)

    (probe,) = hapi.probes
    assert exists is False
    assert probe.url.path == "/fhir/Patient"
    assert probe.url.params["identifier"] == f"{SYNTHEA_IDENTIFIER_SYSTEM}|{UUID_A}"
    assert probe.url.params["_summary"] == "count"
    # HAPI reuses identical searches for 60 s; without this header a stale "0" can cause a re-POST.
    assert probe.headers["Cache-Control"] == "no-cache"


def test_probe_is_true_when_the_patient_exists():
    hapi = FakeHapi(probe_totals=[1])

    assert seed_hapi.probe_patient_exists(hapi.client(), BASE_URL, UUID_A) is True


def test_a_patient_already_on_the_server_is_skipped_and_nothing_is_posted(tmp_path):
    hapi = FakeHapi(probe_totals=[1])

    result = seed_hapi.load_bundle(hapi.client(), BASE_URL, _write_bundle(tmp_path), 30)

    assert result.status == "skipped"
    assert hapi.posts == []


def test_a_new_bundle_is_posted_unchanged_as_fhir_json(tmp_path):
    hapi = FakeHapi(probe_totals=[0])
    path = _write_bundle(tmp_path, entries=3)

    result = seed_hapi.load_bundle(hapi.client(), BASE_URL, path, 30)

    (post,) = hapi.posts
    assert result.status == "loaded"
    assert result.entries == 3
    assert str(post.url) == BASE_URL
    assert post.headers["Content-Type"] == "application/fhir+json"
    assert post.content == path.read_bytes()


def test_a_timeout_that_actually_committed_counts_as_loaded(tmp_path):
    # The POST times out, but the transaction landed: the re-probe finds the patient.
    hapi = FakeHapi(probe_totals=[0, 1], post_error=httpx.ReadTimeout("slow"))

    result = seed_hapi.load_bundle(hapi.client(), BASE_URL, _write_bundle(tmp_path), 30)

    assert result.status == "loaded"
    assert len(hapi.probes) == 2  # once before the POST, once after the error


def test_a_rejected_bundle_that_is_not_on_the_server_fails_with_hapi_diagnostics(tmp_path):
    outcome = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "diagnostics": "Unable to resolve reference urn:uuid:x"}],
    }
    hapi = FakeHapi(probe_totals=[0, 0], post_response=httpx.Response(422, json=outcome))

    result = seed_hapi.load_bundle(hapi.client(), BASE_URL, _write_bundle(tmp_path), 30)

    assert result.status == "failed"
    assert "Unable to resolve reference" in result.detail
    assert len(hapi.probes) == 2
