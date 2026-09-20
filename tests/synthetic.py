# ORIGIN: AI — hand-built FHIR records typed by Claude Code, reviewed by Kiel.
"""Minimal, hand-made FHIR resources for cases the real Synthea sample never produces.

The real sample only contains active/resolved conditions, active/stopped medications and
active/inactive allergies. Everything else the rules must handle (recurrence, relapse, on-hold,
entered-in-error, missing statuses, medicationReference, deceasedBoolean, ...) is built here and
named as synthetic in the tests. Pass `None` for a field to leave it out entirely.
"""

CLINICAL = "http://terminology.hl7.org/CodeSystem/condition-clinical"
VERIFICATION = "http://terminology.hl7.org/CodeSystem/condition-ver-status"


def codeable(code: str, *, system: str = CLINICAL) -> dict:
    return {"coding": [{"system": system, "code": code}]}


def condition(
    id: str | None = "c1",
    *,
    status: str | None = "active",
    verification: str | None = "confirmed",
    onset: str | None = "2020-01-01",
    display: str | None = "Type 2 diabetes",
    **extra,
) -> dict:
    resource: dict = {"resourceType": "Condition"}
    if id is not None:
        resource["id"] = id
    if status is not None:
        resource["clinicalStatus"] = codeable(status)
    if verification is not None:
        resource["verificationStatus"] = codeable(verification, system=VERIFICATION)
    if onset is not None:
        resource["onsetDateTime"] = onset
    if display is not None:
        resource["code"] = {"text": display}
    return {**resource, **extra}


def medication(
    id: str | None = "m1",
    *,
    status: str | None = "active",
    authored: str | None = "2020-01-01",
    drug: str | None = "Metformin 500 MG Oral Tablet",
    **extra,
) -> dict:
    resource: dict = {"resourceType": "MedicationRequest"}
    if id is not None:
        resource["id"] = id
    if status is not None:
        resource["status"] = status
    if authored is not None:
        resource["authoredOn"] = authored
    if drug is not None:
        resource["medicationCodeableConcept"] = {"text": drug}
    return {**resource, **extra}


def allergy(
    id: str | None = "a1",
    *,
    status: str | None = "active",
    verification: str | None = "confirmed",
    criticality: str | None = "low",
    recorded: str | None = "2020-01-01",
    display: str | None = "Penicillin allergy",
    **extra,
) -> dict:
    resource: dict = {"resourceType": "AllergyIntolerance"}
    if id is not None:
        resource["id"] = id
    if status is not None:
        resource["clinicalStatus"] = codeable(status)
    if verification is not None:
        resource["verificationStatus"] = codeable(verification, system=VERIFICATION)
    if criticality is not None:
        resource["criticality"] = criticality
    if recorded is not None:
        resource["recordedDate"] = recorded
    if display is not None:
        resource["code"] = {"text": display}
    return {**resource, **extra}


def patient(
    id: str | None = "p1",
    *,
    names: list[dict] | None = None,
    birth: str | None = "1970-01-01",
    gender: str | None = "female",
    deceased_boolean: bool | None = None,
    deceased_datetime: str | None = None,
    **extra,
) -> dict:
    resource: dict = {"resourceType": "Patient"}
    if id is not None:
        resource["id"] = id
    resource["name"] = (
        names if names is not None else [{"use": "official", "family": "Doe", "given": ["Jane"]}]
    )
    if birth is not None:
        resource["birthDate"] = birth
    if gender is not None:
        resource["gender"] = gender
    if deceased_boolean is not None:
        resource["deceasedBoolean"] = deceased_boolean
    if deceased_datetime is not None:
        resource["deceasedDateTime"] = deceased_datetime
    return {**resource, **extra}
