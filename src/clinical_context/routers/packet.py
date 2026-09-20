# ORIGIN: AI — route typed by Claude Code, reviewed by Kiel. The request path (validate the id,
#   resolve the patient, fetch every page of the three lists, assemble, attach the summary) follows
#   the agreed design; Kiel's error contract lives in main.py.
"""GET /v1/patients/{patient_id}/clinical-context: one patient's sourced packet."""

import asyncio
import logging
import time
from datetime import UTC, date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter
from fastapi import Path as PathParam

from ..assembly import assemble_packet
from ..config import Settings
from ..dependencies import FhirDep, SettingsDep, SummarizerDep
from ..models import ClinicalContextPacket, SummaryBlock, Timings
from ..privacy import error_location, patient_hash
from ..summarizer import SummaryResult

router = APIRouter(prefix="/v1", tags=["clinical context"])
logger = logging.getLogger("clinical_context.request")

# A HAPI id or a Synthea identifier: short, no spaces, no path or query characters.
PATIENT_ID_PATTERN = r"^[A-Za-z0-9.\-]{1,64}$"
PatientId = Annotated[
    str,
    PathParam(
        pattern=PATIENT_ID_PATTERN,
        description="A HAPI Patient id, or an identifier such as the Synthea UUID",
    ),
]


def _reference_date(settings: Settings) -> tuple[date, Literal["now", "config"]]:
    """The date "current" is measured against: configured for a frozen dataset, else today."""
    if settings.as_of_date is not None:
        return settings.as_of_date, "config"
    return datetime.now(UTC).date(), "now"


def _milliseconds_since(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _log_request(
    patient_id: str, packet: ClinicalContextPacket, fhir_ms: int, llm_ms: int | None, total_ms: int
):
    """One line per request: counts and timings only. Never a name, a fact, or an id."""
    meta = packet.meta
    logger.info(
        "packet patient=%s conditions=%d medications=%d allergies=%d "
        "excluded=%d/%d/%d invalid=%d/%d/%d truncated=%s fhir_ms=%d llm_ms=%s total_ms=%d "
        "summary=%s reason=%s",
        patient_hash(patient_id),
        len(packet.conditions),
        len(packet.medications),
        len(packet.allergies),
        meta.excluded_counts.conditions,
        meta.excluded_counts.medications,
        meta.excluded_counts.allergies,
        meta.invalid_counts.conditions,
        meta.invalid_counts.medications,
        meta.invalid_counts.allergies,
        meta.truncated,
        fhir_ms,
        llm_ms,
        total_ms,
        packet.summary.status,
        packet.summary.reason,
    )


# ORIGIN: H-spec — Kiel's decision: the three searches run together, and if one fails we still
#   wait for the others to finish before raising, so no task is left running (and no "exception
#   was never retrieved" noise). Lines typed by Claude Code.
async def _fetch_lists(fhir: FhirDep, fhir_id: str) -> tuple[list[dict], list[dict], list[dict]]:
    results = await asyncio.gather(
        fhir.search_by_patient("Condition", fhir_id),
        fhir.search_by_patient("MedicationRequest", fhir_id),
        fhir.search_by_patient("AllergyIntolerance", fhir_id),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    conditions, medications, allergies = results
    return conditions, medications, allergies


# ORIGIN: H-spec — Kiel's decision: if the summarizer fails in any way, including a bug in it, the
#   reviewer still gets every fact; only the summary is unavailable. Lines typed by Claude Code.
async def _summary_or_unavailable(
    summarize: SummarizerDep, packet: ClinicalContextPacket, settings: Settings
) -> SummaryResult:
    try:
        return await summarize(packet)
    except Exception as error:  # deliberately broad: the facts must survive anything
        logger.error("summarizer crashed: %s at %s", type(error).__name__, error_location(error))
        block = SummaryBlock(
            text=None, status="unavailable", model=settings.ollama_model, reason="internal_error"
        )
        return SummaryResult(block=block, timings=None, elapsed_ms=None, attempts=0)


@router.get("/patients/{patient_id}/clinical-context", response_model=ClinicalContextPacket)
async def clinical_context(
    patient_id: PatientId,
    fhir: FhirDep,
    settings: SettingsDep,
    summarize: SummarizerDep,
) -> ClinicalContextPacket:
    started = time.perf_counter()

    patient = await fhir.resolve_patient(patient_id)
    conditions, medications, allergies = await _fetch_lists(fhir, patient["id"])
    fhir_ms = _milliseconds_since(started)

    as_of, as_of_source = _reference_date(settings)
    packet = assemble_packet(
        patient=patient,
        conditions=conditions,
        medications=medications,
        allergies=allergies,
        as_of=as_of,
        as_of_source=as_of_source,
        generated_at=datetime.now(UTC),
        list_cap=settings.list_cap,
        patient_id_echo=patient_id,
    )

    # The summary is the only thing a model may add. Facts, gaps and meta stay exactly as assembled.
    result = await _summary_or_unavailable(summarize, packet, settings)
    total_ms = _milliseconds_since(started)
    packet = packet.model_copy(
        update={
            "summary": result.block,
            "meta": packet.meta.model_copy(
                update={"timings_ms": Timings(fhir=fhir_ms, llm=result.elapsed_ms, total=total_ms)}
            ),
        }
    )
    _log_request(patient_id, packet, fhir_ms, result.elapsed_ms, total_ms)
    return packet
