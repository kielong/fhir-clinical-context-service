# ORIGIN: AI — script typed by Claude Code, reviewed by Kiel.
"""Read-only, offline inspection of the extracted Synthea sample.

    python scripts/recon_synthea.py [--as-of 2019-09-16] [--today YYYY-MM-DD]

Prints the facts the rest of the system is built on: file and resource counts, how statuses and
dates are actually shaped, how big the charts get, and the expected counts for the priority
patients. Compare the output with what the design assumes; if they differ, the design is wrong.
"""

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from clinical_context.config import Settings
from synthea_data import SYNTHEA_IDENTIFIER_SYSTEM, bundle_files, read_priority, uuid_from_filename

ACTIVE_CONDITION = {"active", "recurrence", "relapse"}
ACTIVE_MEDICATION = {"active", "on-hold"}
SEMANTIC_TAG = re.compile(r"\((finding|situation)\)$")
LIST_CAP = 25


@dataclass
class PatientRow:
    uuid: str
    name: str
    entries: int
    birth: str | None = None
    deceased: str | None = None
    conditions: int = 0
    conditions_active: int = 0
    medications: int = 0
    medications_active: int = 0
    allergies_active: int = 0
    allergies_inactive: int = 0


class Tally(Counter):
    """Counts of (group, key) pairs, so one object holds every distribution we report.

    `add` accepts a bool as the amount, so `tally.add("g", "k", "x" in resource)` counts the
    resources that have the field (and records a 0 when none do).
    """

    def add(self, group: str, key, amount: int = 1) -> None:
        self[(group, key)] += amount

    def group(self, name: str) -> dict:
        return {key: count for (group, key), count in self.items() if group == name}


@dataclass
class Sample:
    rows: list[PatientRow] = field(default_factory=list)
    tally: Tally = field(default_factory=Tally)
    entries_per_bundle: list[int] = field(default_factory=list)
    file_sizes: list[tuple[int, str]] = field(default_factory=list)
    hospital_files: list[str] = field(default_factory=list)
    latest_date: str = ""


def code_of(concept) -> str | None:
    """Status codes are CodeableConcepts in R4; older exports used a bare string."""
    if isinstance(concept, str):
        return concept
    codings = (concept or {}).get("coding") or [{}]
    return codings[0].get("code")


def age_on(birth: str, ref: date, deceased: str | None = None) -> int:
    end = min(ref, date.fromisoformat(deceased[:10])) if deceased else ref
    born = date.fromisoformat(birth[:10])
    return end.year - born.year - ((end.month, end.day) < (born.month, born.day))


def spread(values: list[int]) -> str:
    ordered = sorted(values)
    p90 = ordered[int(len(ordered) * 0.9)]
    median = statistics.median(ordered)
    return f"min {ordered[0]} · median {median:g} · p90 {p90} · max {ordered[-1]}"


def pct(part: int, whole: int) -> str:
    return f"{part:,} ({100 * part / whole:.0f}%)"


# ---------------------------------------------------------------- scanning


def _scan_patient(resource: dict, uuid: str, row: PatientRow, tally: Tally) -> None:
    tally.add("patient", "id_equals_filename_uuid", resource["id"] == uuid)
    for identifier in resource.get("identifier", []):
        tally.add("identifier_system", identifier["system"])
        tally.add("identifier_value_is_uuid", identifier["system"], identifier["value"] == uuid)
    first_name = (resource.get("name") or [{}])[0]
    tally.add("patient", "official_name_first", first_name.get("use") == "official")
    tally.add("patient", "name_prefix", "prefix" in first_name)
    tally.add("patient", "name_text", "text" in first_name)
    tally.add("patient", "deceased_datetime", "deceasedDateTime" in resource)
    tally.add("patient", "deceased_boolean", "deceasedBoolean" in resource)
    row.birth = resource["birthDate"]
    row.deceased = resource.get("deceasedDateTime")


def _scan_condition(resource: dict, row: PatientRow, tally: Tally) -> str:
    row.conditions += 1
    status = code_of(resource.get("clinicalStatus"))
    tally.add("condition_status_type", type(resource.get("clinicalStatus")).__name__)
    tally.add("condition_status", status)
    tally.add("condition_verification", code_of(resource.get("verificationStatus")))
    tally.add("condition_field", "category", "category" in resource)
    for name in ("onsetDateTime", "onsetPeriod", "recordedDate", "abatementDateTime"):
        tally.add("condition_field", name, name in resource)
    if status in ACTIVE_CONDITION:
        row.conditions_active += 1
        tag = SEMANTIC_TAG.search((resource.get("code") or {}).get("text", ""))
        if tag:
            tally.add("active_condition_tag", tag.group(1))
    return resource.get("onsetDateTime", "")


def _scan_medication(resource: dict, row: PatientRow, tally: Tally) -> str:
    row.medications += 1
    tally.add("medication_status", resource.get("status"))
    has_concept = "medicationCodeableConcept" in resource
    tally.add("medication_drug", "codeable" if has_concept else "reference")
    tally.add("medication_field", "authoredOn", "authoredOn" in resource)
    row.medications_active += resource.get("status") in ACTIVE_MEDICATION
    return resource.get("authoredOn", "")


def _scan_allergy(resource: dict, row: PatientRow, tally: Tally) -> None:
    status = code_of(resource.get("clinicalStatus"))
    tally.add("allergy_status", status)
    tally.add("allergy_verification", code_of(resource.get("verificationStatus")))
    tally.add("allergy_criticality", resource.get("criticality"))
    tally.add("allergy_field", "recordedDate", "recordedDate" in resource)
    if status == "active":
        row.allergies_active += 1
    else:
        row.allergies_inactive += 1


def _scan_bundle(path: Path, sample: Sample) -> None:
    tally = sample.tally
    uuid = uuid_from_filename(path)
    bundle = json.loads(path.read_bytes())
    entries = bundle.get("entry", [])
    tally.add("bundle", (bundle.get("resourceType"), bundle.get("type")))
    sample.entries_per_bundle.append(len(entries))
    sample.file_sizes.append((path.stat().st_size, path.name))
    row = PatientRow(uuid=uuid, name=path.name.rsplit("_", 1)[0], entries=len(entries))

    for entry in entries:
        resource = entry["resource"]
        kind = resource["resourceType"]
        request = entry.get("request", {})
        tally.add("resource_type", kind)
        tally.add("request_method", request.get("method"))
        tally.add("entry", "fullUrl_is_urn_uuid", entry.get("fullUrl", "").startswith("urn:uuid:"))
        tally.add("entry", "has_ifNoneExist", "ifNoneExist" in request)

        latest_seen = ""
        if kind == "Organization":
            tally.add("organization_id", resource["id"])
        elif kind == "Patient":
            _scan_patient(resource, uuid, row, tally)
        elif kind == "Condition":
            latest_seen = _scan_condition(resource, row, tally)
        elif kind == "MedicationRequest":
            latest_seen = _scan_medication(resource, row, tally)
        elif kind == "AllergyIntolerance":
            _scan_allergy(resource, row, tally)
        elif kind == "Encounter":
            period = resource.get("period", {})
            latest_seen = period.get("end") or period.get("start") or ""
        sample.latest_date = max(sample.latest_date, latest_seen)
    sample.rows.append(row)


def scan(files: list[Path]) -> Sample:
    """One pass over every bundle, collecting everything the report needs."""
    sample = Sample()
    for path in files:
        if re.match(r"(hospital|practitioner)Information", path.name, re.I):
            sample.hospital_files.append(path.name)
        _scan_bundle(path, sample)
    return sample


# ---------------------------------------------------------------- report


def _print_files(sample: Sample, file_count: int) -> None:
    biggest_bytes, biggest_name = max(sample.file_sizes)
    total_mb = sum(size for size, _ in sample.file_sizes) / 1e6
    print("== FILES ==")
    print(f"patient JSON files: {file_count:,}")
    print(f"bundle (resourceType, type): {sample.tally.group('bundle')}")
    print(f"hospital/practitioner files: {sample.hospital_files or 'none'}")
    print(f"total size: {total_mb:,.0f} MB")
    print(f"largest bundle: {biggest_bytes / 1e6:.1f} MB ({biggest_name[:40]})")


def _print_resources(sample: Sample) -> None:
    tally = sample.tally
    by_type = tally.group("resource_type")
    top = sorted(by_type.items(), key=lambda item: -item[1])[:8]
    orgs = tally.group("organization_id")
    entry = tally.group("entry")
    print("\n== RESOURCES ==")
    print(f"total entries: {sum(sample.entries_per_bundle):,}")
    print("by type: " + ", ".join(f"{kind} {n:,}" for kind, n in top) + ", ...")
    print(f"request methods: {tally.group('request_method')}")
    print(
        f"fullUrl is urn:uuid: {entry['fullUrl_is_urn_uuid']:,}; "
        f"has ifNoneExist: {entry['has_ifNoneExist']}"
    )
    print(
        f"Organization entries {sum(orgs.values()):,} for {len(orgs):,} unique ids "
        f"(max repeat {max(orgs.values())}x); Practitioner entries {by_type['Practitioner']:,}"
    )
    print(f"entries per bundle: {spread(sample.entries_per_bundle)}")


def _print_patients(sample: Sample) -> None:
    tally = sample.tally
    patient = tally.group("patient")
    count = len(sample.rows)
    deceased = patient["deceased_datetime"]
    print("\n== PATIENTS ==")
    print(
        f"living {count - deceased:,}; deceased (deceasedDateTime) {deceased}; "
        f"deceasedBoolean {patient['deceased_boolean']}"
    )
    print(f"Patient.id == filename uuid: {patient['id_equals_filename_uuid']} of {count}")
    print(f"identifier systems: {tally.group('identifier_system')}")
    print(
        f"identifier value == filename uuid, by system: {tally.group('identifier_value_is_uuid')}"
    )
    print(
        f"official name first: {patient['official_name_first']}; "
        f"name.prefix: {patient['name_prefix']}; name.text: {patient['name_text']}"
    )


def _print_conditions(sample: Sample) -> None:
    tally, rows = sample.tally, sample.rows
    fields = tally.group("condition_field")
    dates = {name: n for name, n in fields.items() if name != "category"}
    active = sum(r.conditions_active for r in rows)
    tagged = tally.group("active_condition_tag")
    print("\n== CONDITIONS ==")
    print(
        f"total {sum(r.conditions for r in rows):,}; "
        f"clinicalStatus python type {tally.group('condition_status_type')}"
    )
    print(
        f"clinicalStatus: {tally.group('condition_status')}; "
        f"verificationStatus: {tally.group('condition_verification')}"
    )
    print(f"has category: {fields['category']}; date fields present: {dates}")
    print(f"per patient total: {spread([r.conditions for r in rows])}")
    print(f"per patient active: {spread([r.conditions_active for r in rows])}")
    print(
        f"active with (finding)/(situation) tag: {pct(sum(tagged.values()), active)} "
        f"of {active:,} {tagged}"
    )


def _print_medications(sample: Sample) -> None:
    tally, rows = sample.tally, sample.rows
    print("\n== MEDICATIONS ==")
    print(
        f"total {sum(r.medications for r in rows):,}; status {tally.group('medication_status')}; "
        f"medication as {tally.group('medication_drug')}; "
        f"authoredOn present {tally.group('medication_field')['authoredOn']:,}"
    )
    print(f"per patient total: {spread([r.medications for r in rows])}")
    print(f"per patient active: {spread([r.medications_active for r in rows])}")
    none_active = sum(r.medications_active == 0 for r in rows)
    print(f"patients with no active medication: {pct(none_active, len(rows))}")


def _print_allergies(sample: Sample) -> None:
    tally, rows = sample.tally, sample.rows
    status = tally.group("allergy_status")
    print("\n== ALLERGIES ==")
    print(
        f"total {sum(status.values()):,}; clinicalStatus {status}; "
        f"verificationStatus {tally.group('allergy_verification')}"
    )
    print(
        f"criticality {tally.group('allergy_criticality')}; "
        f"recordedDate present {tally.group('allergy_field')['recordedDate']}"
    )
    none = sum(r.allergies_active + r.allergies_inactive == 0 for r in rows)
    print(f"patients with no allergy: {pct(none, len(rows))}")


def _print_empty_charts_cap_and_pagination(sample: Sample) -> None:
    rows = sample.rows
    empty = [
        r
        for r in rows
        if r.conditions_active == 0 and r.medications_active == 0 and not r.allergies_active
    ]
    reaching_cap = [
        r
        for r in rows
        if max(r.conditions_active, r.medications_active, r.allergies_active) >= LIST_CAP
    ]
    print("\n== EMPTY CHARTS, CAP, PAGINATION ==")
    print(
        f"no active condition, medication, or allergy: {pct(len(empty), len(rows))}; "
        f"no conditions at all: {sum(r.conditions == 0 for r in rows)}"
    )
    print(
        f"max active conditions {max(r.conditions_active for r in rows)}, "
        f"active meds {max(r.medications_active for r in rows)}, "
        f"active allergies {max(r.allergies_active for r in rows)} "
        f"(LIST_CAP={LIST_CAP} reached by {len(reaching_cap)} patients)"
    )
    print(
        f"patients with >20 conditions {sum(r.conditions > 20 for r in rows)}; "
        f"meds >20 {sum(r.medications > 20 for r in rows)}, "
        f">100 {sum(r.medications > 100 for r in rows)}, "
        f">200 {sum(r.medications > 200 for r in rows)}; "
        f"most meds {max(r.medications for r in rows):,}"
    )


def _print_reference_date(sample: Sample, as_of: date, today: date) -> None:
    living = [r for r in sample.rows if not r.deceased]
    minors = [r for r in living if age_on(r.birth, as_of) < 18]
    flipped = [r for r in minors if age_on(r.birth, today) >= 18]
    very_old = [r for r in living if age_on(r.birth, today) >= 110]
    oldest = max(age_on(r.birth, today) for r in living)
    print("\n== REFERENCE DATE ==")
    print(f"latest date in the data: {sample.latest_date[:10]}")
    print(f"living patients under 18 at {as_of}: {len(minors)}")
    print(f"  of those, aged 18+ on {today}: {len(flipped)}")
    print(f"living patients aged 110+ on {today}: {len(very_old)} (oldest {oldest})")


def _print_priority(sample: Sample, priority: list[str], as_of: date) -> None:
    by_uuid = {r.uuid: r for r in sample.rows}
    print(f"\n== PRIORITY PATIENTS (ages at {as_of}) ==")
    print(
        f"{'patient':24} {'age':>4} {'conds tot/act':>14} {'meds tot/act':>13} "
        f"{'allergy act/inact':>18} {'entries':>8}"
    )
    for uuid in priority:
        row = by_uuid.get(uuid)
        if row is None:
            print(f"{uuid}: NOT IN THE DATA")
            continue
        age = age_on(row.birth, as_of, row.deceased)
        note = " (deceased)" if row.deceased else ""
        print(
            f"{row.name[:24]:24} {age:>4} "
            f"{row.conditions:>7}/{row.conditions_active:<6} "
            f"{row.medications:>6}/{row.medications_active:<6} "
            f"{row.allergies_active:>9}/{row.allergies_inactive:<8} {row.entries:>8}{note}"
        )
    print(f"\n(identifier system used for lookups: {SYNTHEA_IDENTIFIER_SYSTEM})")


def report(sample: Sample, file_count: int, priority: list[str], as_of: date, today: date) -> None:
    _print_files(sample, file_count)
    _print_resources(sample)
    _print_patients(sample)
    _print_conditions(sample)
    _print_medications(sample)
    _print_allergies(sample)
    _print_empty_charts_cap_and_pagination(sample)
    _print_reference_date(sample, as_of, today)
    _print_priority(sample, priority, as_of)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2019, 9, 16))
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    args = parser.parse_args(argv)

    settings = Settings()
    files = bundle_files(settings.seed_data_dir)
    if not files:
        print(f"no bundles in {settings.seed_data_dir}; run scripts/download_synthea.py first")
        return 1
    priority = read_priority(settings.seed_priority_file)
    report(scan(files), len(files), priority, args.as_of, args.today)
    return 0


if __name__ == "__main__":
    sys.exit(main())
