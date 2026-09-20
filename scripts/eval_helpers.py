# ORIGIN: AI — helper code typed by Claude Code from the agreed evaluation checks (a source must
#   resolve, belong to this patient, and carry the status the packet claims), reviewed by Kiel.
#   What counts as "schema-valid" and the requirement that a fetched resource really is the one
#   named in the source are Claude Code's additions, not yet adopted.
"""The pure parts of the evaluation: what to check in a packet, and how to add results up.

No network here, so all of it is unit-tested. scripts/eval_ten.py does the fetching and printing.
"""

import math
import random
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from synthea_data import uuid_from_filename

Kind = Literal["patient", "condition", "medication", "allergy"]
SECTIONS = ("conditions", "medications", "allergies")

_RESOURCE_TYPE = {
    "patient": "Patient",
    "condition": "Condition",
    "medication": "MedicationRequest",
    "allergy": "AllergyIntolerance",
}


@dataclass(frozen=True)
class Claim:
    """One source the packet cites, and the status the packet says that record has."""

    kind: Kind
    source: str
    status: str | None
    label: str  # the display text, only so a person can read the table


@dataclass(frozen=True)
class SourceCheck:
    claim: Claim
    resolved: bool
    subject_ok: bool
    status_ok: bool | None  # None: nothing to check (the patient record has no status)
    display_ok: bool | None = None  # the fact's text is one the record itself carries


def claims_of(packet: dict) -> list[Claim]:
    claims = [Claim("patient", packet["patient"]["source"], None, "")]
    for fact in packet["conditions"]:
        claims.append(Claim("condition", fact["source"], fact["clinical_status"], fact["display"]))
    for fact in packet["medications"]:
        claims.append(Claim("medication", fact["source"], fact["status"], fact["display"]))
    for fact in packet["allergies"]:
        claims.append(Claim("allergy", fact["source"], fact["clinical_status"], fact["display"]))
    return claims


def status_of(resource: dict) -> str | None:
    """The status a FHIR record carries: MedicationRequest.status, else clinicalStatus."""
    if resource.get("resourceType") == "MedicationRequest":
        return resource.get("status")
    codings = (resource.get("clinicalStatus") or {}).get("coding") or []
    return codings[0].get("code") if codings else None


# What assembly falls back to when a record has no name at all.
_NAMELESS = {"unknown", "unknown medication"}


def record_names(resource: dict) -> set[str]:
    """Every name a record carries for what it describes: its text, and each coding's display
    and code (a medication may name itself by reference instead)."""
    names: set[str] = set()
    for concept in (resource.get("code"), resource.get("medicationCodeableConcept")):
        if not concept:
            continue
        if concept.get("text"):
            names.add(concept["text"])
        for coding in concept.get("coding") or []:
            names.update(v for v in (coding.get("display"), coding.get("code")) if v)
    reference = resource.get("medicationReference") or {}
    names.update(v for v in (reference.get("display"), reference.get("reference")) if v)
    return names


def _display_ok(label: str, resource: dict) -> bool:
    names = record_names(resource)
    return label in names or (not names and label in _NAMELESS)


def _owner_reference(resource: dict) -> str | None:
    """Who the record is about: AllergyIntolerance uses `patient`, the others use `subject`."""
    field_name = "patient" if resource.get("resourceType") == "AllergyIntolerance" else "subject"
    return (resource.get(field_name) or {}).get("reference")


def check_claim(claim: Claim, resource: dict | None, patient_fhir_id: str) -> SourceCheck:
    """Compare what the packet cited with the record HAPI returns for it (None = not found)."""
    applicable = claim.kind != "patient"
    type_name, _, record_id = claim.source.partition("/")
    if (
        resource is None
        or resource.get("resourceType") != type_name
        or resource.get("id") != record_id
        or type_name != _RESOURCE_TYPE[claim.kind]
    ):
        return SourceCheck(
            claim, False, False, False if applicable else None, False if applicable else None
        )

    if claim.kind == "patient":
        subject_ok = resource.get("id") == patient_fhir_id
    else:
        subject_ok = _owner_reference(resource) == f"Patient/{patient_fhir_id}"
    status_ok = status_of(resource) == claim.status if applicable else None
    display_ok = _display_ok(claim.label, resource) if applicable else None
    return SourceCheck(claim, True, subject_ok, status_ok, display_ok)


@dataclass(frozen=True)
class SectionAccount:
    """How many records HAPI holds for a patient in one section, and how many the packet
    accounts for (shown, cut by the cap, left out as not active, invalid, or unreadable)."""

    section: str
    hapi_total: int
    accounted: int

    @property
    def ok(self) -> bool:
        return self.hapi_total == self.accounted


_CUT = re.compile(r"showing \d+ of (\d+) included")
_UNREADABLE = re.compile(r"^(\d+) ")


def account_for(packet: dict, hapi_totals: dict[str, int]) -> list[SectionAccount]:
    """Independent of the service: does every record HAPI holds show up somewhere in the packet?"""
    meta = packet["meta"]
    accounts = []
    for section in SECTIONS:
        shown = len(packet[section])
        accounted = shown + meta["excluded_counts"][section] + meta["invalid_counts"][section]
        for item in packet["missing"]:
            if item["section"] != section:
                continue
            if item["code"] == "truncated_section" and (cut := _CUT.search(item["detail"])):
                accounted += int(cut.group(1)) - shown
            elif item["code"] == "unparseable_resource" and (
                n := _UNREADABLE.match(item["detail"])
            ):
                accounted += int(n.group(1))
        accounts.append(SectionAccount(section, hapi_totals[section], accounted))
    return accounts


# ORIGIN: AI — Claude Code's proposal, not yet adopted: cheap word patterns that point a person at
#   summaries worth reading closely. They decide nothing; a person marks fairness.
_PRESENT_TENSE = re.compile(
    r"\b(?:is|are)\s+(?:currently\s+|now\s+)?(?:taking|being\s+treated|receiving|prescribed)\b"
    r"|\bcurrently\s+(?:has|have|takes|taking|on|receiving)\b",
    re.IGNORECASE,
)
_SAYS_DECEASED = re.compile(r"\b(?:deceased|died|dead|death|passed\s+away)\b", re.IGNORECASE)


def wording_flags(text: str, *, deceased: bool) -> list[str]:
    flags = []
    if deceased:
        if not _SAYS_DECEASED.search(text):
            flags.append("deceased_not_stated")
        if _PRESENT_TENSE.search(text):
            flags.append("deceased_present_tense")
    elif _PRESENT_TENSE.search(text):
        flags.append("present_tense")
    return flags


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """The range a true rate probably lies in, given `successes` out of `n` (95% by default)."""
    if n == 0:
        return None
    p = successes / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, center - half), min(1.0, center + half)


def percentile(values: Iterable[float], p: float) -> float | None:
    """Nearest rank: always a value that really happened. None for no values."""
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def sample_uuids(files: Iterable[str | Path], n: int, seed: int) -> list[str]:
    """`n` distinct Synthea identifiers from bundle filenames; the same seed gives the same ones
    whatever order the files are listed in."""
    uuids = sorted(uuid_from_filename(f) for f in files)
    return random.Random(seed).sample(uuids, min(n, len(uuids)))


@dataclass
class PatientResult:
    uuid: str
    http_status: int
    summary_status: str | None  # None when the API gave no packet (404, 502, ...)
    summary_reason: str | None
    counts: tuple[int, int, int]  # conditions, medications, allergies included in the packet
    excluded: tuple[int, int, int]  # ...and left out as no longer active
    wall_ms: int  # from the first request until the reviewer had a summary (or we gave up)
    checks: list[SourceCheck] = field(default_factory=list)
    text_violation: str | None = None  # a word rule the returned summary text breaks, if any
    flags: list[str] = field(default_factory=list)  # wording that deserves a person's eye
    accounts: list[SectionAccount] = field(default_factory=list)
    cached_before: bool = False  # the service already had this summary: its time says nothing
    problem: str | None = None  # why the script could not finish this patient


@dataclass
class BatchSummary:
    patients: int = 0
    not_answered: int = 0  # no packet at all (not loaded, or HAPI down)
    generated: int = 0
    unavailable_by_reason: dict[str, int] = field(default_factory=dict)
    text_violations: dict[str, int] = field(default_factory=dict)
    usable_rate: float | None = None  # patients with a packet who got a summary
    cached_before: int = 0
    flag_counts: dict[str, int] = field(default_factory=dict)
    flagged: dict[str, list[str]] = field(default_factory=dict)
    completeness_checked: int = 0
    completeness_ok: int = 0
    incomplete: list[str] = field(default_factory=list)
    display_checked: int = 0
    display_ok: int = 0
    problems: dict[str, str] = field(default_factory=dict)
    answered_by_model: int = 0
    schema_valid: int = 0
    schema_valid_rate: float | None = None
    sources_checked: int = 0
    resolved: int = 0
    subject_ok: int = 0
    status_checked: int = 0
    status_ok: int = 0
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    empty_rate: dict[str, float] = field(default_factory=dict)
    excluded_p50: dict[str, float] = field(default_factory=dict)
    excluded_p95: dict[str, float] = field(default_factory=dict)
    excluded_max: dict[str, int] = field(default_factory=dict)


# Reasons where the model did answer, so its output can be judged; a timeout or a model that was
# not running says nothing about what it writes.
_MODEL_ANSWERED = {"invalid_output", "policy_violation"}


def summarize_batch(results: list[PatientResult]) -> BatchSummary:
    batch = BatchSummary(patients=len(results))
    answered = [r for r in results if r.http_status == 200]
    batch.not_answered = len(results) - len(answered)

    for r in results:
        if r.problem:
            batch.problems[r.uuid] = r.problem
    for r in answered:
        batch.cached_before += r.cached_before
        for flag in r.flags:
            batch.flag_counts[flag] = batch.flag_counts.get(flag, 0) + 1
            batch.flagged.setdefault(flag, []).append(r.uuid)
        batch.completeness_checked += len(r.accounts)
        batch.completeness_ok += sum(account.ok for account in r.accounts)
        if not all(account.ok for account in r.accounts):
            batch.incomplete.append(r.uuid)
        if r.summary_status == "generated":
            batch.generated += 1
        elif r.summary_reason:
            reasons = batch.unavailable_by_reason
            reasons[r.summary_reason] = reasons.get(r.summary_reason, 0) + 1
        if r.text_violation:
            hits = batch.text_violations
            hits[r.text_violation] = hits.get(r.text_violation, 0) + 1
        for check in r.checks:
            batch.sources_checked += 1
            batch.resolved += check.resolved
            batch.subject_ok += check.subject_ok
            if check.status_ok is not None:
                batch.status_checked += 1
                batch.status_ok += check.status_ok
            if check.display_ok is not None:
                batch.display_checked += 1
                batch.display_ok += check.display_ok

    invalid = batch.unavailable_by_reason.get("invalid_output", 0)
    batch.answered_by_model = batch.generated + sum(
        count for reason, count in batch.unavailable_by_reason.items() if reason in _MODEL_ANSWERED
    )
    batch.schema_valid = batch.answered_by_model - invalid
    if batch.answered_by_model:
        batch.schema_valid_rate = batch.schema_valid / batch.answered_by_model

    if answered:
        batch.usable_rate = batch.generated / len(answered)
    # A summary the service already remembered took no time to produce, so it says nothing here.
    generated_ms = [
        r.wall_ms for r in answered if r.summary_status == "generated" and not r.cached_before
    ]
    batch.latency_p50_ms = percentile(generated_ms, 50)
    batch.latency_p95_ms = percentile(generated_ms, 95)

    for index, section in enumerate(SECTIONS):
        if not answered:
            break
        batch.empty_rate[section] = sum(r.counts[index] == 0 for r in answered) / len(answered)
        excluded = [r.excluded[index] for r in answered]
        batch.excluded_p50[section] = percentile(excluded, 50)
        batch.excluded_p95[section] = percentile(excluded, 95)
        batch.excluded_max[section] = max(excluded)
    return batch
