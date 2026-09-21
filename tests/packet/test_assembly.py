# ORIGIN: AI — test cases typed by Claude Code from the agreed rules, reviewed by Kiel.
"""assemble_packet and its helpers: the rules that decide what a reviewer sees.

"real" tests use excerpts of the Synthea sample (tests/fixtures/real); "synthetic" tests use
hand-built records (tests/synthetic.py) for rules the real sample never triggers.
"""

import json
from datetime import UTC, date, datetime

import pytest

import synthetic as syn
from clinical_context.packet.assembly import (
    assemble_packet,
    code_of,
    condition_onset,
    display_codeable,
    medication_display,
    patient_age_years,
    patient_display_name,
)
from clinical_context.packet.models import ClinicalContextPacket
from packets import FIXTURES

AS_OF = date(2019, 9, 16)  # the Synthea sample is frozen at this date
GENERATED_AT = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def assemble(*, patient=None, conditions=(), medications=(), allergies=(), **overrides):
    arguments = {
        "patient": patient if patient is not None else syn.patient(),
        "conditions": list(conditions),
        "medications": list(medications),
        "allergies": list(allergies),
        "as_of": AS_OF,
        "as_of_source": "config",
        "generated_at": GENERATED_AT,
        "list_cap": 25,
        "patient_id_echo": "demo-id",
    }
    return assemble_packet(**{**arguments, **overrides})


def real(name: str, **overrides) -> ClinicalContextPacket:
    fixture = json.loads((FIXTURES / f"{name}.json").read_text())
    return assemble(
        patient=fixture["patient"],
        conditions=fixture["conditions"],
        medications=fixture["medications"],
        allergies=fixture["allergies"],
        **overrides,
    )


def missing_items(packet, code: str, section: str | None = None):
    return [
        m for m in packet.missing if m.code == code and (section is None or m.section == section)
    ]


def displays(facts) -> list[str]:
    return [fact.display for fact in facts]


# ============================================================ conditions: include / exclude


def test_active_condition_included_resolved_excluded_real():
    packet = real("Aaron697_Brekke496")  # 10 conditions: 5 active, 5 resolved

    assert len(packet.conditions) == 5
    assert packet.meta.excluded_counts.conditions == 5
    assert all(c.clinical_status == "active" for c in packet.conditions)


def test_clinical_status_is_read_from_a_codeable_concept():
    assert code_of({"coding": [{"system": "s", "code": "active"}]}) == "active"
    assert code_of("active") == "active"  # older exports used a bare string
    assert code_of(None) is None
    assert code_of({"coding": []}) is None
    assert code_of({"text": "no coding"}) is None


@pytest.mark.parametrize("status", ["recurrence", "relapse"])
def test_recurrence_and_relapse_are_included_synthetic(status):
    packet = assemble(conditions=[syn.condition("c1", status=status)])

    assert [c.clinical_status for c in packet.conditions] == [status]
    assert packet.meta.excluded_counts.conditions == 0


@pytest.mark.parametrize("status", ["resolved", "inactive", "remission"])
def test_other_statuses_are_excluded_and_counted_synthetic(status):
    packet = assemble(conditions=[syn.condition("c1", status=status)])

    assert packet.conditions == []
    assert packet.meta.excluded_counts.conditions == 1


def test_entered_in_error_condition_is_dropped_and_counted_as_invalid_not_excluded():
    # An entered-in-error record was never true, so it is not "history". It is dropped, but we
    # still count it apart from excluded history so the reviewer can see records were dropped.
    packet = assemble(
        conditions=[
            syn.condition("c1", status="active", verification="entered-in-error"),
            syn.condition("c2", status="active"),
        ]
    )

    assert [c.source for c in packet.conditions] == ["Condition/c2"]
    assert packet.meta.invalid_counts.conditions == 1
    assert packet.meta.excluded_counts.conditions == 0


def test_condition_with_no_clinical_status_is_excluded_and_counted_synthetic():
    packet = assemble(conditions=[syn.condition("c1", status=None)])

    assert packet.conditions == []
    assert packet.meta.excluded_counts.conditions == 1


# ============================================================ medications: include / exclude


def test_meds_active_and_on_hold_included_stopped_and_others_excluded():
    packet = assemble(
        medications=[
            syn.medication("m1", status="active"),
            syn.medication("m2", status="on-hold"),  # synthetic: the sample has no on-hold
            syn.medication("m3", status="stopped"),
            syn.medication("m4", status="completed"),
            syn.medication("m5", status=None),  # no status at all
        ]
    )

    assert sorted(m.status for m in packet.medications) == ["active", "on-hold"]
    assert packet.meta.excluded_counts.medications == 3


def test_real_stopped_medication_is_excluded():
    packet = real("Aaron697_Brekke496")  # one stopped ibuprofen prescription

    assert packet.medications == []
    assert packet.meta.excluded_counts.medications == 1


def test_entered_in_error_medication_is_dropped_and_counted_as_invalid():
    packet = assemble(medications=[syn.medication("m1", status="entered-in-error")])

    assert packet.medications == []
    assert packet.meta.invalid_counts.medications == 1
    assert packet.meta.excluded_counts.medications == 0


# ============================================================ allergies: include / exclude


def test_inactive_allergy_is_excluded_and_counted_real():
    packet = real("Andreas188_Dare640")  # 5 active allergies and 1 inactive (dairy)

    assert len(packet.allergies) == 5
    assert packet.meta.excluded_counts.allergies == 1
    assert "Allergy to dairy product" not in displays(packet.allergies)


def test_allergy_with_no_clinical_status_is_still_shown_with_null_status():
    # Safety bias: never hide an allergy just because its status was not recorded.
    packet = assemble(allergies=[syn.allergy("a1", status=None)])

    assert [a.source for a in packet.allergies] == ["AllergyIntolerance/a1"]
    assert packet.allergies[0].clinical_status is None


@pytest.mark.parametrize("status", ["inactive", "resolved"])
def test_inactive_or_resolved_allergy_is_excluded_synthetic(status):
    packet = assemble(allergies=[syn.allergy("a1", status=status)])

    assert packet.allergies == []
    assert packet.meta.excluded_counts.allergies == 1


def test_entered_in_error_allergy_is_dropped_and_counted_as_invalid():
    packet = assemble(allergies=[syn.allergy("a1", verification="entered-in-error")])

    assert packet.allergies == []
    assert packet.meta.invalid_counts.allergies == 1
    assert packet.meta.excluded_counts.allergies == 0


def test_allergy_criticality_passes_through_and_is_null_when_absent():
    packet = assemble(
        allergies=[
            syn.allergy("a1", criticality="high"),
            syn.allergy("a2", criticality=None),
        ]
    )

    by_source = {a.source: a.criticality for a in packet.allergies}
    assert by_source == {"AllergyIntolerance/a1": "high", "AllergyIntolerance/a2": None}


# ============================================================ missing


def test_empty_section_says_how_many_others_were_excluded_real():
    packet = real("Aaron697_Brekke496")

    (meds,) = missing_items(packet, "empty_section", "medications")
    assert meds.detail == (
        "No active or on-hold MedicationRequest resources; "
        "1 other on file was excluded as not active"
    )
    (allergies,) = missing_items(packet, "empty_section", "allergies")
    assert allergies.detail == "No AllergyIntolerance resources on file"
    assert missing_items(packet, "empty_section", "conditions") == []  # it has 5


def test_empty_section_uses_plural_wording_for_several_excluded_synthetic():
    packet = assemble(conditions=[syn.condition(f"c{i}", status="resolved") for i in range(3)])

    (conditions,) = missing_items(packet, "empty_section", "conditions")
    assert conditions.detail == (
        "No active, recurrence, or relapse Condition resources; "
        "3 others on file were excluded as not active"
    )


def test_a_patient_with_an_empty_chart_gets_three_empty_sections_and_no_crash_real():
    packet = real("Alicia629_Walter473")  # a child: no conditions, medications or allergies

    sections = [m.section for m in missing_items(packet, "empty_section")]
    assert sections == ["conditions", "medications", "allergies"]
    assert packet.missing[0].detail == "No Condition resources on file"
    assert packet.patient.age_years == 2


def test_missing_items_come_in_a_stable_order():
    packet = assemble(
        patient=syn.patient(deceased_datetime="2018-01-01T00:00:00Z"),
        conditions=[syn.condition(f"c{i}", onset=f"20{i:02d}-01-01") for i in range(4)],
        list_cap=3,
    )

    codes = [(m.code, m.section) for m in packet.missing]
    assert codes == [
        ("patient_deceased", "patient"),
        ("truncated_section", "conditions"),
        ("empty_section", "medications"),
        ("empty_section", "allergies"),
    ]


# ============================================================ sort then cap


def test_conditions_come_newest_onset_first_with_ties_broken_by_display_real():
    packet = real("Aaron697_Brekke496")

    assert displays(packet.conditions) == [
        "Anemia (disorder)",  # 1987-12-21 (ties with Prediabetes: alphabetical)
        "Prediabetes",  # 1987-12-21
        "Body mass index 30+ - obesity (finding)",  # 1977-02-21
        "Cardiac Arrest",  # 1965-11-15 (ties: alphabetical)
        "History of cardiac arrest (situation)",  # 1965-11-15
    ]


def test_cap_marks_truncated_and_says_how_many_were_cut_synthetic():
    conditions = [syn.condition(f"c{i:02d}", onset=f"2000-01-{i + 1:02d}") for i in range(26)]

    packet = assemble(conditions=conditions)

    assert len(packet.conditions) == 25
    assert packet.meta.truncated is True
    (cut,) = missing_items(packet, "truncated_section")
    assert cut.section == "conditions"
    assert cut.detail == "showing 25 of 26 included Condition resources, newest first"


def test_cap_keeps_the_most_recent_and_drops_the_oldest_synthetic():
    conditions = [syn.condition(f"c{i:02d}", onset=f"2000-01-{i + 1:02d}") for i in range(26)]

    packet = assemble(conditions=conditions)

    kept = {c.source for c in packet.conditions}
    assert "Condition/c00" not in kept  # the oldest onset is the one dropped
    assert "Condition/c25" in kept  # the newest is kept


def test_a_list_exactly_at_the_cap_is_not_truncated():
    conditions = [syn.condition(f"c{i:02d}", onset=f"2000-01-{i + 1:02d}") for i in range(25)]

    packet = assemble(conditions=conditions)

    assert len(packet.conditions) == 25
    assert packet.meta.truncated is False
    assert missing_items(packet, "truncated_section") == []


def test_cap_of_three_on_a_real_patient_with_24_active_conditions():
    packet = real("Shelly431_Corwin846", list_cap=3)

    assert len(packet.conditions) == 3
    assert packet.meta.truncated is True
    # Shelly also has 7 active medications, so the cap of 3 cuts that list too.
    (cut,) = missing_items(packet, "truncated_section", "conditions")
    assert cut.detail == "showing 3 of 24 included Condition resources, newest first"
    assert len(packet.medications) == 3


def test_the_cap_applies_to_medications_and_allergies_too():
    packet = assemble(
        medications=[syn.medication(f"m{i}", authored=f"2020-01-0{i + 1}") for i in range(5)],
        allergies=[syn.allergy(f"a{i}", recorded=f"2020-01-0{i + 1}") for i in range(5)],
        list_cap=2,
    )

    assert [m.source for m in packet.medications] == [
        "MedicationRequest/m4",
        "MedicationRequest/m3",
    ]
    assert [a.source for a in packet.allergies] == [
        "AllergyIntolerance/a4",
        "AllergyIntolerance/a3",
    ]
    assert {m.section for m in missing_items(packet, "truncated_section")} == {
        "medications",
        "allergies",
    }


def test_medications_are_newest_first_real():
    packet = real("Jose871_Williamson769")  # 14 active medications

    dates = [m.authored_on for m in packet.medications]
    assert dates == sorted(dates, reverse=True)
    assert dates[0] == "2017-06-15"


def test_null_dates_sort_last_and_ties_are_deterministic_synthetic():
    conditions = [
        syn.condition("c1", onset=None, display="Zeta"),
        syn.condition("c2", onset="2020-01-01", display="Beta"),
        syn.condition("c3", onset="2020-01-01", display="Alpha"),
        syn.condition("c4", onset="2021-06-01", display="Gamma"),
        syn.condition("c5", onset=None, display="Alpha"),
        syn.condition("c7", onset="2020-01-01", display="Alpha"),
    ]

    forward = assemble(conditions=conditions)
    backward = assemble(conditions=list(reversed(conditions)))

    assert [c.source for c in forward.conditions] == [
        "Condition/c4",  # newest
        "Condition/c3",  # 2020-01-01, display Alpha, source c3
        "Condition/c7",  # 2020-01-01, display Alpha, source c7 (tie broken by source)
        "Condition/c2",  # 2020-01-01, display Beta
        "Condition/c5",  # no date: last, alphabetical
        "Condition/c1",
    ]
    assert forward.conditions == backward.conditions  # input order never changes the output


# ============================================================ identity: deceased and age


def test_deceased_patient_gets_a_gap_and_age_at_death_real():
    packet = real("Floyd420_Jerde200")  # born 1917-08-06, died 2013-02-10

    assert packet.patient.deceased is True
    assert packet.patient.deceased_date == "2013-02-10"
    assert packet.patient.age_years == 95
    (gap,) = missing_items(packet, "patient_deceased")
    assert gap.section == "patient"
    assert gap.detail == (
        "Patient deceased 2013-02-10; statuses below are the last recorded and are not current"
    )


def test_second_deceased_patient_age_at_death_real():
    assert real("Jose871_Williamson769").patient.age_years == 93  # died 2017-07-31


def test_deceased_boolean_without_a_date_is_still_deceased_synthetic():
    packet = assemble(patient=syn.patient(deceased_boolean=True))

    assert packet.patient.deceased is True
    assert packet.patient.deceased_date is None
    (gap,) = missing_items(packet, "patient_deceased")
    assert gap.detail == (
        "Patient is recorded as deceased; statuses below are the last recorded and are not current"
    )


def test_deceased_boolean_false_is_not_deceased_synthetic():
    packet = assemble(patient=syn.patient(deceased_boolean=False))

    assert packet.patient.deceased is False
    assert missing_items(packet, "patient_deceased") == []


def test_living_patient_has_no_deceased_gap_real():
    packet = real("Aaron697_Brekke496")

    assert packet.patient.deceased is False
    assert packet.patient.deceased_date is None
    assert missing_items(packet, "patient_deceased") == []


def test_age_uses_the_as_of_date_not_today_real():
    fixture = json.loads((FIXTURES / "Alan320_Wiza601.json").read_text())  # born 2014-10-26

    assert patient_age_years(fixture["patient"], date(2019, 9, 16)) == 4
    assert patient_age_years(fixture["patient"], date(2026, 9, 18)) == 11


@pytest.mark.parametrize(
    ("birth", "expected"),
    [("2000-09-16", 19), ("2000-09-17", 18)],  # the birthday itself counts; the day before does not
)
def test_age_turns_over_on_the_birthday(birth, expected):
    assert patient_age_years(syn.patient(birth=birth), date(2019, 9, 16)) == expected


def test_age_stops_at_death_and_ignores_a_death_after_as_of():
    died_before = syn.patient(birth="1950-06-01", deceased_datetime="2000-06-01T00:00:00Z")
    dies_later = syn.patient(birth="1950-06-01", deceased_datetime="2030-06-01T00:00:00Z")

    assert patient_age_years(died_before, date(2019, 9, 16)) == 50
    assert patient_age_years(dies_later, date(2019, 9, 16)) == 69  # alive as of the reference date


def test_age_is_null_without_a_usable_birth_date():
    as_of = date(2019, 9, 16)

    assert patient_age_years(syn.patient(birth=None), as_of) is None
    assert patient_age_years(syn.patient(birth="2030-01-01"), as_of) is None  # born after as_of
    assert patient_age_years(syn.patient(birth="1970"), as_of) is None  # year-only: do not guess


@pytest.mark.parametrize("birth", ["2020-13-45", "1990-02-30", "0000-00-00"])
def test_a_birth_date_that_looks_complete_but_is_not_a_real_day_gives_no_age(birth):
    assert patient_age_years(syn.patient(birth=birth), date(2019, 9, 16)) is None


def test_a_death_date_that_is_not_a_real_day_is_ignored_rather_than_used():
    patient = syn.patient(birth="1950-06-01", deceased_datetime="2000-02-30T00:00:00Z")

    assert patient_age_years(patient, date(2019, 9, 16)) == 69  # measured to the reference date


# ============================================================ display rules


def test_codeable_concept_prefers_text_then_display_then_code_then_unknown():
    assert display_codeable({"text": "T", "coding": [{"display": "D", "code": "C"}]}) == "T"
    assert display_codeable({"coding": [{"display": "D", "code": "C"}]}) == "D"
    assert display_codeable({"coding": [{"code": "C"}]}) == "C"
    assert display_codeable({"coding": []}) == "unknown"
    assert display_codeable({}) == "unknown"
    assert display_codeable(None) == "unknown"


def test_medication_display_falls_back_through_the_reference():
    assert medication_display({"medicationCodeableConcept": {"text": "Metformin"}}) == "Metformin"
    assert (
        medication_display(
            {"medicationReference": {"reference": "Medication/9", "display": "Aspirin"}}
        )
        == "Aspirin"
    )
    # A reference with no display: name the resource, never fetch it.
    assert (
        medication_display({"medicationReference": {"reference": "Medication/9"}}) == "Medication/9"
    )
    assert medication_display({"medicationReference": {"reference": "urn:uuid:abc"}}) == (
        "unknown medication"
    )
    assert medication_display({}) == "unknown medication"


def test_an_empty_medication_concept_falls_back_to_the_reference():
    resource = {
        "medicationCodeableConcept": {},
        "medicationReference": {"reference": "Medication/9", "display": "Aspirin"},
    }

    assert medication_display(resource) == "Aspirin"


def test_medication_reference_shows_up_in_the_packet_synthetic():
    reference_only = {
        "resourceType": "MedicationRequest",
        "id": "m9",
        "status": "active",
        "medicationReference": {"reference": "Medication/9", "display": "Aspirin 81 MG"},
    }

    packet = assemble(medications=[reference_only])

    assert displays(packet.medications) == ["Aspirin 81 MG"]


def test_display_name_omits_the_prefix_and_keeps_synthea_digits_real():
    assert real("Aaron697_Brekke496").patient.display_name == "Aaron697 Brekke496"


def test_display_name_prefers_official_then_usual_then_the_first_synthetic():
    official_second = [
        {"use": "maiden", "family": "Old", "given": ["Jane"]},
        {"use": "official", "family": "New", "given": ["Jane"]},
    ]
    usual_no_official = [
        {"use": "nickname", "family": "Nick", "given": ["J"]},
        {"use": "usual", "family": "Usual", "given": ["Jay"]},
    ]
    neither = [{"use": "nickname", "family": "First", "given": ["A"]}, {"family": "Second"}]

    assert patient_display_name(syn.patient(names=official_second)) == "Jane New"
    assert patient_display_name(syn.patient(names=usual_no_official)) == "Jay Usual"
    assert patient_display_name(syn.patient(names=neither)) == "A First"


def test_display_name_uses_text_when_present_and_joins_given_names():
    with_text = [{"text": "Dr. Jane Doe", "family": "Ignored", "given": ["Ignored"]}]
    two_given = [{"family": "Smith", "given": ["Mary", "Ann"], "prefix": ["Ms."]}]

    assert patient_display_name(syn.patient(names=with_text)) == "Dr. Jane Doe"
    assert patient_display_name(syn.patient(names=two_given)) == "Mary Ann Smith"


def test_display_name_is_null_when_there_is_nothing_to_show():
    assert patient_display_name(syn.patient(names=[])) is None
    assert patient_display_name(syn.patient(names=[{}])) is None


def test_onset_falls_back_from_datetime_to_period_start_to_recorded_date():
    assert condition_onset({"onsetDateTime": "2019-05-03T10:00:00-05:00"}) == "2019-05-03"
    assert condition_onset({"onsetPeriod": {"start": "2018-01-02T00:00:00Z"}}) == "2018-01-02"
    assert condition_onset({"recordedDate": "2017-03-04T00:00:00Z"}) == "2017-03-04"
    assert (
        condition_onset({"onsetDateTime": "2019-05-03", "recordedDate": "2010-01-01"})
        == "2019-05-03"
    )
    assert condition_onset({}) is None


def test_a_year_only_onset_date_is_kept_as_written():
    assert condition_onset({"onsetDateTime": "2015"}) == "2015"


# ============================================================ robustness


def test_a_resource_with_no_id_is_skipped_and_reported_not_fatal():
    packet = assemble(
        conditions=[syn.condition("c1"), syn.condition(None), syn.condition("c3")],
    )

    assert [c.source for c in packet.conditions] == ["Condition/c1", "Condition/c3"]  # tie: source
    (gap,) = missing_items(packet, "unparseable_resource", "conditions")
    assert gap.detail == "1 Condition resource could not be read and was skipped"
    assert packet.meta.excluded_counts.conditions == 0  # skipped is not "excluded history"


def test_a_malformed_resource_is_skipped_and_reported_not_fatal():
    malformed = syn.condition("bad", clinicalStatus=5)  # a number where a CodeableConcept belongs

    packet = assemble(conditions=[malformed, malformed | {"id": "bad2"}, syn.condition("ok")])

    assert [c.source for c in packet.conditions] == ["Condition/ok"]
    (gap,) = missing_items(packet, "unparseable_resource", "conditions")
    assert gap.detail == "2 Condition resources could not be read and were skipped"


def test_unparseable_medications_and_allergies_are_reported_per_section():
    packet = assemble(
        medications=[syn.medication(None)],
        allergies=[syn.allergy(None), syn.allergy(None)],
    )

    assert [m.section for m in missing_items(packet, "unparseable_resource")] == [
        "medications",
        "allergies",
    ]


def test_a_patient_without_an_id_is_an_error():
    with pytest.raises(ValueError, match="id"):
        assemble(patient=syn.patient(None))


# ============================================================ packet-level


def test_source_is_resource_type_slash_id_never_a_url():
    packet = real("Andreas188_Dare640")

    assert all(c.source.startswith("Condition/") for c in packet.conditions)
    assert all(m.source.startswith("MedicationRequest/") for m in packet.medications)
    assert all(a.source.startswith("AllergyIntolerance/") for a in packet.allergies)
    every = [f.source for f in (*packet.conditions, *packet.medications, *packet.allergies)]
    assert all("http" not in s and s.count("/") == 1 for s in every)
    assert packet.patient.source == f"Patient/{packet.patient.fhir_id}"


def test_identity_echoes_the_caller_id_and_uses_the_resource_id():
    packet = assemble(patient=syn.patient("42", gender="male", birth="1980-02-03"))

    assert packet.patient_id == "demo-id"
    assert packet.patient.fhir_id == "42"
    assert packet.patient.source == "Patient/42"
    assert packet.patient.gender == "male"
    assert packet.patient.birth_date == "1980-02-03"


def test_summary_is_unavailable_because_assembly_never_calls_a_model():
    packet = assemble()

    assert packet.summary.status == "unavailable"
    assert packet.summary.text is None
    assert packet.summary.model is None
    assert packet.summary.reason is None
    assert packet.meta.timings_ms is None


def test_meta_says_which_status_rule_and_sort_decided_what_you_see():
    packet = assemble(list_cap=7)

    included = packet.meta.included
    assert included.conditions == (
        "clinicalStatus in active, recurrence, relapse; newest onset first; max 7"
    )
    assert included.medications == "status in active, on-hold; newest authoredOn first; max 7"
    assert included.allergies == (
        "clinicalStatus active or unrecorded; newest recordedDate first; max 7"
    )


def test_meta_echoes_the_injected_dates_and_source():
    packet = assemble(as_of_source="now")

    assert packet.meta.as_of == AS_OF
    assert packet.meta.as_of_source == "now"
    assert packet.meta.generated_at == GENERATED_AT


def test_assembly_is_deterministic_real():
    first = real("Lorenzo669_Cuellar188")
    second = real("Lorenzo669_Cuellar188")

    assert first.model_dump_json() == second.model_dump_json()


def test_models_reject_unknown_fields():
    packet = assemble()
    payload = packet.model_dump()
    payload["approved"] = True  # the service never decides; the schema must not allow it either

    with pytest.raises(ValueError, match="approved"):
        ClinicalContextPacket.model_validate(payload)
