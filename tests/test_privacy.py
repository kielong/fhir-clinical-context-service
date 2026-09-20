# ORIGIN: AI — test cases typed by Claude Code, reviewed by Kiel.
"""Nothing that identifies a patient may reach a log: not an id in a path, not a guessable hash."""

import hashlib
import logging

import pytest

from clinical_context.main import app  # noqa: F401  (importing the app wires up the logging rules)
from clinical_context.privacy import RedactPatientIds, patient_hash

UUID = "2fa15bc7-8866-461a-9000-f739e425860a"


def _access_record(path: str) -> logging.LogRecord:
    """The record uvicorn's access logger emits: (client, method, path, http version, status)."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("10.0.0.5:51234", "GET", path, "1.1", 200),
        exc_info=None,
    )


def _text(record: logging.LogRecord) -> str:
    RedactPatientIds().filter(record)
    return record.getMessage()


# ============================================================ the hash


def test_the_hash_is_short_and_stable_within_one_process():
    assert patient_hash(UUID) == patient_hash(UUID)
    assert len(patient_hash(UUID)) == 8


def test_different_ids_get_different_hashes():
    assert patient_hash("1000") != patient_hash("1001")


def test_the_hash_cannot_be_reversed_by_trying_likely_ids():
    # An unkeyed sha256 of a guessable id (a HAPI id like 1000, an SSN) can be found by hashing
    # every candidate. A keyed hash cannot: the key exists only inside this process.
    assert patient_hash("1000") != hashlib.sha256(b"1000").hexdigest()[:8]
    assert patient_hash(UUID) != hashlib.sha256(UUID.encode()).hexdigest()[:8]


# ============================================================ redacting the access log


@pytest.mark.parametrize(
    "path",
    [
        f"/v1/patients/{UUID}/clinical-context",
        "/v1/patients/1000/clinical-context",
        "/v1/patients/a.b-c/clinical-context",
        "/v1/patients/a%20b/clinical-context",  # a rejected id is still an id
        f"/v1/patients/{UUID}/clinical-context?pretty=1",
    ],
)
def test_the_patient_id_is_removed_from_an_access_log_line(path):
    text = _text(_access_record(path))

    assert "/v1/patients/{id}/clinical-context" in text
    for private in (UUID, "1000", "a.b-c", "a%20b"):
        assert private not in text


def test_everything_else_in_the_access_line_is_kept():
    text = _text(_access_record(f"/v1/patients/{UUID}/clinical-context"))

    assert text == '10.0.0.5:51234 - "GET /v1/patients/{id}/clinical-context HTTP/1.1" 200'


def test_paths_without_a_patient_id_are_untouched():
    assert "GET /health HTTP/1.1" in _text(_access_record("/health"))
    assert "/openapi.json" in _text(_access_record("/openapi.json"))


def test_an_id_inside_a_plain_message_is_redacted_too():
    record = logging.LogRecord(
        "uvicorn.error", logging.INFO, __file__, 0, "bad request /v1/patients/1000/x", (), None
    )

    assert "1000" not in _text(record)


# ============================================================ wired in, not just available


def test_the_running_app_redacts_uvicorns_access_logger(caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.access")

    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "10.0.0.5:1",
        "GET",
        f"/v1/patients/{UUID}/clinical-context",
        "1.1",
        200,
    )

    assert UUID not in caplog.text
    assert "/v1/patients/{id}/clinical-context" in caplog.text


@pytest.mark.parametrize("name", ["httpx", "httpcore"])
def test_the_http_client_libraries_do_not_log_request_urls(name):
    # httpx logs every request URL at INFO, and our URLs contain the patient id.
    assert logging.getLogger(name).level >= logging.WARNING
