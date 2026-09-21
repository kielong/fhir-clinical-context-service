# ORIGIN: H-spec — the error contract is Kiel's decision (404 no such patient, 409 duplicate
#   identifier, 422 malformed id, 502 FHIR trouble, 500 anything else; every body a fixed sentence).
#   Kiel also decided to write it down as a model and as OpenAPI responses, so a client can read
#   the contract from /docs. Lines typed by Claude Code.
"""The error contract, in one place: the body, the fixed sentences, and how the API documents them.

Every error body is `{"detail": "<a fixed sentence>"}`. Nothing the caller sent and nothing from the
record is ever put in it, so it is safe to show and to log.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

PATIENT_NOT_FOUND = "Patient not found"
AMBIGUOUS_IDENTIFIER = "More than one patient matches this identifier"
MALFORMED_PATIENT_ID = "patient_id must be 1-64 characters: letters, digits, '.' or '-'"
FHIR_UNAVAILABLE = "The FHIR server is unavailable"
INTERNAL_ERROR = "Internal server error"


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: str


def _documented(description: str, detail: str) -> dict[str, Any]:
    return {
        "model": ErrorResponse,
        "description": description,
        "content": {"application/json": {"example": {"detail": detail}}},
    }


# For the route's `responses=`. The 422 here replaces FastAPI's default one, whose body repeats the
# rejected input back to the caller; ours never does.
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: _documented("No patient has this id or identifier.", PATIENT_NOT_FOUND),
    409: _documented(
        "The identifier matches more than one patient. Nothing is picked for you.",
        AMBIGUOUS_IDENTIFIER,
    ),
    422: _documented("The patient id is malformed. It was never looked up.", MALFORMED_PATIENT_ID),
    502: _documented(
        "The FHIR server is down, too slow, failed, or a search needed more pages than allowed.",
        FHIR_UNAVAILABLE,
    ),
    500: _documented(
        "Anything unexpected. The body is generic; the cause is only in the log.", INTERNAL_ERROR
    ),
}
