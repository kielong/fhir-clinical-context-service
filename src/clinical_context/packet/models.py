# ORIGIN: AI — schema typed by Claude Code from the agreed packet shape, reviewed by Kiel.
#   The shape itself (which fields exist, the summary object, structured `missing`, the excluded
#   and invalid counts) is Kiel's decision.
"""The packet the API returns.

Everything here except `summary.text` is assembled in code from FHIR records. The model never
sees a `source`, never writes a fact, and the schema rejects fields it does not know about, so
nothing like an "approved" flag can be added by accident.

Dates are kept as the FHIR date text (`YYYY`, `YYYY-MM` or `YYYY-MM-DD`) because real records
often carry only a year, and a `date` type would either crash on them or invent a month.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

Section = Literal["patient", "conditions", "medications", "allergies"]
MissingCode = Literal[
    "empty_section", "truncated_section", "patient_deceased", "unparseable_resource"
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PatientIdentity(_Strict):
    fhir_id: str
    display_name: str | None
    birth_date: str | None
    age_years: int | None
    gender: str | None
    deceased: bool
    deceased_date: str | None
    source: str


class ConditionFact(_Strict):
    display: str
    clinical_status: Literal["active", "recurrence", "relapse"]
    onset_date: str | None
    source: str


class MedicationFact(_Strict):
    display: str
    status: Literal["active", "on-hold"]
    authored_on: str | None
    source: str


class AllergyFact(_Strict):
    display: str
    clinical_status: Literal["active"] | None  # None: the record has no status; shown anyway
    criticality: str | None
    source: str


# Why a summary is unavailable. `internal_error` means our own code failed while writing it (a bug,
# not the model); the facts are still returned.
SummaryReason = Literal[
    "model_unreachable", "timeout", "invalid_output", "policy_violation", "internal_error"
]


class SummaryBlock(_Strict):
    text: str | None
    status: Literal["generated", "unavailable"]
    model: str | None
    reason: SummaryReason | None


class MissingItem(_Strict):
    code: MissingCode
    section: Section
    detail: str


class IncludedRules(_Strict):
    """Plain-language statement of the filter and sort applied to each list."""

    conditions: str
    medications: str
    allergies: str


class SectionCounts(_Strict):
    conditions: int = 0
    medications: int = 0
    allergies: int = 0


class Timings(_Strict):
    fhir: int | None = None
    llm: int | None = None
    total: int | None = None


class PacketMeta(_Strict):
    as_of: date
    as_of_source: Literal["now", "config"]
    generated_at: datetime
    included: IncludedRules
    excluded_counts: SectionCounts  # real records left out because they are not active
    invalid_counts: SectionCounts  # entered-in-error records dropped
    truncated: bool
    timings_ms: Timings | None = None


class ClinicalContextPacket(_Strict):
    patient_id: str
    patient: PatientIdentity
    conditions: list[ConditionFact]
    medications: list[MedicationFact]
    allergies: list[AllergyFact]
    summary: SummaryBlock
    missing: list[MissingItem]
    meta: PacketMeta
