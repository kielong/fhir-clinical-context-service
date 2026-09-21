# ORIGIN: H-spec — the rules below are Kiel's decisions: which conditions, medications and
#   allergies count as current, that entered-in-error records are dropped but counted apart from
#   history, that an allergy of unknown status is still shown, sort by recency then cap, the
#   `missing` gaps, and that a resource that cannot be read is skipped and reported instead of
#   failing the packet, plus four edge cases (marked where they occur). Lines typed by Claude Code.
"""Turn raw FHIR records into a packet. Pure functions: no network, no model, no clock.

Every rule is deterministic. The reference date (`as_of`) and `generated_at` are passed in, so the
same records always produce the same packet.

WHAT COUNTS AS CURRENT
  Conditions   clinicalStatus active, recurrence or relapse -> included.
               Anything else (resolved, inactive, remission, missing) -> excluded, counted.
  Medications  status active or on-hold -> included.
               Anything else (stopped, completed, missing) -> excluded, counted.
  Allergies    clinicalStatus active, or missing -> included (never hide an allergy just because
               nobody recorded its status; it is shown with a null status).
               inactive or resolved -> excluded, counted.
  All three    verificationStatus (status, for medications) entered-in-error -> dropped and
               counted as INVALID. Invalid is not history, so it is never counted as excluded,
               but we still say that records were dropped.

WHAT THE REVIEWER SEES
  Each list is sorted newest first (conditions by onset, medications by authoredOn, allergies by
  recordedDate; no date sorts last; ties break by display, then source), THEN cut to the cap.
  Sorting before cutting is what makes the survivors the most recent ones, not an arbitrary page.
  `source` is "ResourceType/id" taken from the stored resource, set here in code and never by a
  model.

WHAT IS MISSING (computed here, in this order: patient, conditions, medications, allergies)
  patient_deceased     the patient is deceased; their "active" statuses are the last recorded.
  empty_section        a list is empty, worded differently for "nothing on file" and "nothing
                       active, but N others were excluded", so we never claim "no medications on
                       file" for a patient with four stopped prescriptions.
  truncated_section    the cap cut a list: "showing 25 of N".
  unparseable_resource a record that could not be read was skipped; it is reported, not fatal.

HOW WE READ THE FIELDS
  Status fields are CodeableConcepts: use the first coding's code.
  Names   HumanName.text if present, else given + family (no prefix, digits untouched); prefer
          use=official, then usual, then the first name.
  Codes   text, else the first coding's display, else its code, else "unknown".
  Meds    medicationCodeableConcept, else medicationReference.display, else "Medication/{id}",
          else "unknown medication". The Medication resource is never fetched.
  Deceased  deceasedBoolean is true OR deceasedDateTime is present.
  Age     whole years from birthDate to the earlier of as_of and the date of death.
  Onset   onsetDateTime, else onsetPeriod.start, else recordedDate, else none (date part only).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from .models import (
    AllergyFact,
    ClinicalContextPacket,
    ConditionFact,
    IncludedRules,
    ListName,
    MedicationFact,
    MissingItem,
    PacketMeta,
    PatientIdentity,
    SectionCounts,
    SummaryBlock,
)

ACTIVE_CONDITION_STATUSES = frozenset({"active", "recurrence", "relapse"})
ACTIVE_MEDICATION_STATUSES = frozenset({"active", "on-hold"})
ENTERED_IN_ERROR = "entered-in-error"

type Fact = ConditionFact | MedicationFact | AllergyFact


class Disposition(StrEnum):
    INCLUDED = "included"
    EXCLUDED = "excluded"  # real, but not current
    INVALID = "invalid"  # entered in error


# ------------------------------------------------------------------ reading fields


# ORIGIN: H-spec — Kiel's decision: the first coding that actually has a code wins, and a bare
#   string (older exports) is accepted as the code. Lines typed by Claude Code.
def code_of(concept: str | dict[str, Any] | None) -> str | None:
    """The code of a CodeableConcept (or None)."""
    if isinstance(concept, str):
        return concept
    if not concept:
        return None
    for coding in concept.get("coding") or []:
        if coding.get("code"):
            return coding["code"]
    return None


def display_codeable(concept: dict[str, Any] | None) -> str:
    if not concept:
        return "unknown"
    coding = (concept.get("coding") or [{}])[0]
    return concept.get("text") or coding.get("display") or coding.get("code") or "unknown"


def medication_display(resource: dict) -> str:
    name = display_codeable(resource.get("medicationCodeableConcept"))
    if name != "unknown":
        return name
    reference = resource.get("medicationReference") or {}
    if reference.get("display"):
        return reference["display"]
    if str(reference.get("reference", "")).startswith("Medication/"):
        return reference["reference"]
    return "unknown medication"


def _date_part(value: object) -> str | None:
    """`2019-05-03T10:00:00-05:00` -> `2019-05-03`. A year-only `2015` is kept as written."""
    return value[:10] if isinstance(value, str) and value else None


def condition_onset(resource: dict) -> str | None:
    period = resource.get("onsetPeriod") or {}
    return _date_part(
        resource.get("onsetDateTime") or period.get("start") or resource.get("recordedDate")
    )


def medication_authored_on(resource: dict) -> str | None:
    return _date_part(resource.get("authoredOn"))


def allergy_recorded_on(resource: dict) -> str | None:
    return _date_part(resource.get("recordedDate"))


def _preferred_name(names: list[dict]) -> dict | None:
    for use in ("official", "usual"):
        for name in names:
            if name.get("use") == use:
                return name
    return names[0] if names else None


def patient_display_name(patient: dict) -> str | None:
    name = _preferred_name(patient.get("name") or [])
    if not name:
        return None
    if name.get("text"):
        return name["text"]
    parts = [*(name.get("given") or []), name.get("family")]
    return " ".join(part for part in parts if part) or None


def patient_deceased(patient: dict) -> bool:
    return patient.get("deceasedBoolean") is True or bool(patient.get("deceasedDateTime"))


def patient_deceased_date(patient: dict) -> str | None:
    return _date_part(patient.get("deceasedDateTime"))


# ORIGIN: H-spec — Kiel's decision: a birth date that is not a full YYYY-MM-DD (for example
#   year-only) gives no age, because guessing a month could be off by a year. Lines typed by
#   Claude Code.
def _full_date(text: str | None) -> date | None:
    if not text or len(text) != 10:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def patient_age_years(patient: dict, as_of: date) -> int | None:
    birth = _full_date(_date_part(patient.get("birthDate")))
    if birth is None:
        return None
    end = as_of
    died = _full_date(patient_deceased_date(patient))
    if died is not None:
        end = min(end, died)
    if end < birth:
        return None
    return end.year - birth.year - ((end.month, end.day) < (birth.month, birth.day))


# ------------------------------------------------------------------ what counts as current


def condition_disposition(resource: dict) -> Disposition:
    if code_of(resource.get("verificationStatus")) == ENTERED_IN_ERROR:
        return Disposition.INVALID
    if code_of(resource.get("clinicalStatus")) in ACTIVE_CONDITION_STATUSES:
        return Disposition.INCLUDED
    return Disposition.EXCLUDED


def medication_disposition(resource: dict) -> Disposition:
    status = resource.get("status")
    if status == ENTERED_IN_ERROR:
        return Disposition.INVALID
    if status in ACTIVE_MEDICATION_STATUSES:
        return Disposition.INCLUDED
    return Disposition.EXCLUDED


# ORIGIN: H-spec — Kiel's decision: a status code other than active/inactive/resolved is treated
#   as "not active" (excluded); only a truly missing status is shown as unknown. Lines typed by
#   Claude Code.
def allergy_disposition(resource: dict) -> Disposition:
    if code_of(resource.get("verificationStatus")) == ENTERED_IN_ERROR:
        return Disposition.INVALID
    status = code_of(resource.get("clinicalStatus"))
    return Disposition.INCLUDED if status in (None, "active") else Disposition.EXCLUDED


# ------------------------------------------------------------------ building the sections


def _source(resource: dict) -> str:
    """`Condition/123`: the stored resource's type and id, never a URL."""
    if not resource.get("id"):
        raise ValueError("resource has no id")
    return f"{resource['resourceType']}/{resource['id']}"


# The status comes from the record, so it is validated against the model, not assumed. An
# unexpected value raises a ValidationError (a ValueError), and the record is reported unreadable.
def _condition_candidate(resource: dict, source: str) -> tuple[str | None, ConditionFact]:
    fact = ConditionFact.model_validate(
        {
            "display": display_codeable(resource.get("code")),
            "clinical_status": code_of(resource.get("clinicalStatus")),
            "onset_date": condition_onset(resource),
            "source": source,
        }
    )
    return fact.onset_date, fact


def _medication_candidate(resource: dict, source: str) -> tuple[str | None, MedicationFact]:
    fact = MedicationFact.model_validate(
        {
            "display": medication_display(resource),
            "status": resource["status"],
            "authored_on": medication_authored_on(resource),
            "source": source,
        }
    )
    return fact.authored_on, fact


def _allergy_candidate(resource: dict, source: str) -> tuple[str | None, AllergyFact]:
    fact = AllergyFact.model_validate(
        {
            "display": display_codeable(resource.get("code")),
            "clinical_status": code_of(resource.get("clinicalStatus")),
            "criticality": resource.get("criticality"),
            "source": source,
        }
    )
    return allergy_recorded_on(resource), fact  # the date only orders the list; it is not exposed


@dataclass
class Section[F: Fact]:
    """One list after filtering, sorting and capping. `F` is the kind of fact the list holds."""

    shown: list[F]
    included_total: int  # how many qualified before the cap
    excluded: int  # real but not current
    invalid: int  # entered-in-error, dropped
    unparseable: int  # could not be read, skipped

    @property
    def truncated(self) -> bool:
        return len(self.shown) < self.included_total


def _newest_first[F: Fact](candidates: list[tuple[str | None, F]]) -> list[F]:
    # Two stable sorts: ties by display then source first, then newest date first. A missing date
    # becomes "" so it sorts last in the descending pass.
    ordered = sorted(candidates, key=lambda c: (c[1].display, c[1].source))
    ordered.sort(key=lambda c: c[0] or "", reverse=True)
    return [fact for _, fact in ordered]


# ORIGIN: H-spec — Kiel's decision: a record that cannot be read is skipped and reported, never
#   fatal. Which exception types mean "cannot be read" is Claude Code's choice.
def _build_section[F: Fact](
    resources: list[dict],
    disposition_of: Callable[[dict], Disposition],
    to_candidate: Callable[[dict, str], tuple[str | None, F]],
    list_cap: int,
) -> Section[F]:
    candidates: list[tuple[str | None, F]] = []
    excluded = invalid = unparseable = 0
    for resource in resources:
        try:
            source = _source(resource)
            disposition = disposition_of(resource)
            if disposition is Disposition.INVALID:
                invalid += 1
            elif disposition is Disposition.EXCLUDED:
                excluded += 1
            else:
                candidates.append(to_candidate(resource, source))
        except (KeyError, TypeError, AttributeError, ValueError):
            unparseable += 1
    newest_first = _newest_first(candidates)
    return Section(newest_first[:list_cap], len(newest_first), excluded, invalid, unparseable)


# ------------------------------------------------------------------ what is missing

_RESOURCE_NAME: dict[ListName, str] = {
    "conditions": "Condition",
    "medications": "MedicationRequest",
    "allergies": "AllergyIntolerance",
}
_CURRENT_PHRASE: dict[ListName, str] = {
    "conditions": "active, recurrence, or relapse Condition",
    "medications": "active or on-hold MedicationRequest",
    "allergies": "active AllergyIntolerance",
}


# ORIGIN: H-spec — Kiel's decision: singular/plural wording ("1 other ... was"), and the deceased
#   gap's text when only a deceasedBoolean (no date) is known. Lines typed by Claude Code.
def _empty_detail(section: ListName, excluded: int) -> str:
    if excluded == 0:
        return f"No {_RESOURCE_NAME[section]} resources on file"
    others = "1 other on file was" if excluded == 1 else f"{excluded} others on file were"
    return f"No {_CURRENT_PHRASE[section]} resources; {others} excluded as not active"


def _unparseable_detail(section: ListName, count: int) -> str:
    name = _RESOURCE_NAME[section]
    if count == 1:
        return f"1 {name} resource could not be read and was skipped"
    return f"{count} {name} resources could not be read and were skipped"


def _deceased_detail(deceased_date: str | None) -> str:
    who = (
        f"Patient deceased {deceased_date}" if deceased_date else "Patient is recorded as deceased"
    )
    return f"{who}; statuses below are the last recorded and are not current"


def compute_missing(
    *,
    deceased: bool,
    deceased_date: str | None,
    sections: dict[ListName, Section[Any]],
    list_cap: int,
) -> list[MissingItem]:
    missing: list[MissingItem] = []
    if deceased:
        missing.append(
            MissingItem(
                code="patient_deceased", section="patient", detail=_deceased_detail(deceased_date)
            )
        )
    for name, section in sections.items():
        if not section.shown:
            detail = _empty_detail(name, section.excluded)
            missing.append(MissingItem(code="empty_section", section=name, detail=detail))
        if section.truncated:
            detail = (
                f"showing {list_cap} of {section.included_total} included "
                f"{_RESOURCE_NAME[name]} resources, newest first"
            )
            missing.append(MissingItem(code="truncated_section", section=name, detail=detail))
        if section.unparseable:
            detail = _unparseable_detail(name, section.unparseable)
            missing.append(MissingItem(code="unparseable_resource", section=name, detail=detail))
    return missing


# ------------------------------------------------------------------ the packet


def _included_rules(list_cap: int) -> IncludedRules:
    cap = f"max {list_cap}"
    return IncludedRules(
        conditions=f"clinicalStatus in active, recurrence, relapse; newest onset first; {cap}",
        medications=f"status in active, on-hold; newest authoredOn first; {cap}",
        allergies=f"clinicalStatus active or unrecorded; newest recordedDate first; {cap}",
    )


def assemble_packet(
    *,
    patient: dict,
    conditions: list[dict],
    medications: list[dict],
    allergies: list[dict],
    as_of: date,
    as_of_source: Literal["now", "config"],
    generated_at: datetime,
    list_cap: int,
    patient_id_echo: str,
) -> ClinicalContextPacket:
    """Build the packet. Summary is left unavailable: the model is filled in elsewhere."""
    patient_source = _source(patient)  # a patient without an id is an error, not a gap
    deceased = patient_deceased(patient)
    deceased_date = patient_deceased_date(patient)

    condition_section = _build_section(
        conditions, condition_disposition, _condition_candidate, list_cap
    )
    medication_section = _build_section(
        medications, medication_disposition, _medication_candidate, list_cap
    )
    allergy_section = _build_section(allergies, allergy_disposition, _allergy_candidate, list_cap)
    sections: dict[ListName, Section[Any]] = {
        "conditions": condition_section,
        "medications": medication_section,
        "allergies": allergy_section,
    }

    return ClinicalContextPacket(
        patient_id=patient_id_echo,
        patient=PatientIdentity(
            fhir_id=patient["id"],
            display_name=patient_display_name(patient),
            birth_date=_date_part(patient.get("birthDate")),
            age_years=patient_age_years(patient, as_of),
            gender=patient.get("gender"),
            deceased=deceased,
            deceased_date=deceased_date,
            source=patient_source,
        ),
        conditions=condition_section.shown,
        medications=medication_section.shown,
        allergies=allergy_section.shown,
        summary=SummaryBlock(text=None, status="unavailable", model=None, reason=None),
        missing=compute_missing(
            deceased=deceased, deceased_date=deceased_date, sections=sections, list_cap=list_cap
        ),
        meta=PacketMeta(
            as_of=as_of,
            as_of_source=as_of_source,
            generated_at=generated_at,
            included=_included_rules(list_cap),
            excluded_counts=SectionCounts(**{n: s.excluded for n, s in sections.items()}),
            invalid_counts=SectionCounts(**{n: s.invalid for n, s in sections.items()}),
            truncated=any(section.truncated for section in sections.values()),
        ),
    )
