# ORIGIN: AI — app wiring and lifespan typed by Claude Code, reviewed by Kiel.
#   Kiel's decision: main.py only wires things together (lifespan, routers, the error contract);
#   routes live in routers/ and shared dependencies in dependencies.py, the conventional FastAPI
#   layout. The error contract itself is labeled H-spec below.
"""FastAPI app entry point: `uvicorn clinical_context.main:app`."""

import logging
import traceback
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .fhir_client import AmbiguousPatient, FhirUnavailable, PatientNotFound
from .privacy import RedactPatientIds, patient_hash
from .routers import health, packet

logger = logging.getLogger("clinical_context.request")


def _configure_logging() -> None:
    """Send our own INFO lines to stderr, and keep patient ids out of every log."""
    root = logging.getLogger("clinical_context")
    root.setLevel(logging.INFO)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(handler)

    # uvicorn's access log prints each request path, and the path contains the patient id.
    for name in ("uvicorn.access", "uvicorn.error"):
        target = logging.getLogger(name)
        if not any(isinstance(f, RedactPatientIds) for f in target.filters):
            target.addFilter(RedactPatientIds())

    # httpx logs every request URL at INFO, and our FHIR URLs contain the patient id.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


_configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One shared async client for the life of the process: nothing blocks the event loop.
    async with httpx.AsyncClient() as client:
        app.state.http = client
        yield


app = FastAPI(title="Clinical Context Packet Service", lifespan=lifespan)
app.include_router(health.router)
app.include_router(packet.router)


# ---------------------------------------------------------------------------------- error contract
#
# ORIGIN: H-spec — the error contract is Kiel's decision:
#   422  the patient id is malformed (never looked up, never echoed back)
#   404  no such patient
#   409  an identifier matches more than one patient (duplicates; we will not pick one)
#   502  the FHIR server is down, too slow, failed, or a search needed more pages than allowed
#   500  anything unexpected, with a generic body
#   200  the facts, even when the summary could not be written (that is `summary.status`)
# Every body is a fixed sentence. Lines typed by Claude Code.


def _who(request: Request) -> str:
    return patient_hash(str(request.path_params.get("patient_id", "")))


@app.exception_handler(PatientNotFound)
async def _not_found(request: Request, exc: PatientNotFound) -> JSONResponse:
    logger.info("patient not found patient=%s", _who(request))
    return JSONResponse({"detail": "Patient not found"}, status_code=404)


@app.exception_handler(AmbiguousPatient)
async def _ambiguous(request: Request, exc: AmbiguousPatient) -> JSONResponse:
    logger.warning("identifier matches several patients patient=%s", _who(request))
    return JSONResponse(
        {"detail": "More than one patient matches this identifier"}, status_code=409
    )


@app.exception_handler(FhirUnavailable)
async def _fhir_unavailable(request: Request, exc: FhirUnavailable) -> JSONResponse:
    logger.warning("fhir unavailable patient=%s reason=%s", _who(request), exc)
    return JSONResponse({"detail": "The FHIR server is unavailable"}, status_code=502)


# ORIGIN: H-spec — Kiel's decision: FastAPI's default 422 body repeats the rejected input back to
#   the caller. An id can be something sensitive typed by mistake (an SSN), so the body is a fixed
#   sentence instead. Lines typed by Claude Code.
@app.exception_handler(RequestValidationError)
async def _bad_request(request: Request, exc: RequestValidationError) -> JSONResponse:
    detail = "patient_id must be 1-64 characters: letters, digits, '.' or '-'"
    return JSONResponse({"detail": detail}, status_code=422)


# ORIGIN: H-spec — Kiel's decision: the catch-all is middleware (not an Exception handler) so the
#   error is not re-raised for the server to print with its message; and the log records only the
#   exception's type and where it happened, because a message can contain data. Lines typed by
#   Claude Code.
@app.middleware("http")
async def _catch_unexpected_errors(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as error:
        last = traceback.extract_tb(error.__traceback__)[-1]
        logger.error(
            "unexpected %s at %s:%s patient=%s",
            type(error).__name__,
            last.filename.rsplit("/", 1)[-1],
            last.lineno,
            _who(request),
        )
        return JSONResponse({"detail": "Internal server error"}, status_code=500)
