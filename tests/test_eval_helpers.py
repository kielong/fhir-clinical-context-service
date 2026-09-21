# ORIGIN: AI — test cases typed by Claude Code from the agreed evaluation checks (a source must
#   resolve, belong to this patient, and carry the status the packet claims), reviewed by Kiel.
#   Cases marked (AI) are additions Claude Code made and Kiel has not adopted yet.
"""The pure parts of the evaluation script: what to check, and how to add the results up."""

import pytest

from eval_helpers import (
    Claim,
    PatientResult,
    SectionAccount,
    SourceCheck,
    account_for,
    check_claim,
    claims_of,
    percentile,
    sample_uuids,
    status_of,
    summarize_batch,
    wilson_interval,
    wording_flags,
)

PATIENT_ID = "1000"
SECTIONS = ("conditions", "medications", "allergies")


def _packet(**overrides) -> dict:
    packet = {
        "patient": {"fhir_id": PATIENT_ID, "source": f"Patient/{PATIENT_ID}"},
        "conditions": [
            {"display": "Anemia (disorder)", "clinical_status": "active", "source": "Condition/c1"},
            {"display": "Asthma", "clinical_status": "recurrence", "source": "Condition/c2"},
        ],
        "medications": [
            {"display": "Warfarin", "status": "on-hold", "source": "MedicationRequest/m1"}
        ],
        "allergies": [
            {"display": "Latex", "clinical_status": "active", "source": "AllergyIntolerance/a1"},
            {"display": "Dust", "clinical_status": None, "source": "AllergyIntolerance/a2"},
        ],
    }
    return {**packet, **overrides}


# ---- what the packet claims


def test_every_source_in_the_packet_is_a_claim_with_the_status_the_packet_gives_it():
    claims = claims_of(_packet())

    assert [(c.kind, c.source, c.status) for c in claims] == [
        ("patient", "Patient/1000", None),
        ("condition", "Condition/c1", "active"),
        ("condition", "Condition/c2", "recurrence"),
        ("medication", "MedicationRequest/m1", "on-hold"),
        ("allergy", "AllergyIntolerance/a1", "active"),
        ("allergy", "AllergyIntolerance/a2", None),
    ]
    assert claims[1].label == "Anemia (disorder)"  # so a person can read the table


def test_an_empty_chart_has_only_the_patient_claim():
    empty = _packet(conditions=[], medications=[], allergies=[])

    assert [c.kind for c in claims_of(empty)] == ["patient"]


# ---- what a fetched resource says about itself


@pytest.mark.parametrize(
    ("resource", "expected"),
    [
        (
            {"resourceType": "Condition", "clinicalStatus": {"coding": [{"code": "active"}]}},
            "active",
        ),
        (
            {
                "resourceType": "AllergyIntolerance",
                "clinicalStatus": {"coding": [{"code": "active"}]},
            },
            "active",
        ),
        ({"resourceType": "MedicationRequest", "status": "on-hold"}, "on-hold"),
        ({"resourceType": "AllergyIntolerance"}, None),  # no status recorded
        ({"resourceType": "Condition", "clinicalStatus": {"coding": []}}, None),
        ({"resourceType": "Patient", "id": "1"}, None),
    ],
)
def test_status_comes_from_the_field_each_resource_type_uses(resource, expected):
    assert status_of(resource) == expected


# ---- one source: does it resolve, is it this patient's, does the status match


def _condition(**overrides) -> dict:
    resource = {
        "resourceType": "Condition",
        "id": "c1",
        "code": {"text": "Anemia"},
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "subject": {"reference": f"Patient/{PATIENT_ID}"},
    }
    return {**resource, **overrides}


CONDITION_CLAIM = Claim("condition", "Condition/c1", "active", "Anemia")


def test_a_matching_condition_passes_every_check():
    check = check_claim(CONDITION_CLAIM, _condition(), PATIENT_ID)

    assert check == SourceCheck(
        CONDITION_CLAIM, resolved=True, subject_ok=True, status_ok=True, display_ok=True
    )


def test_a_source_that_does_not_exist_fails_every_check():
    check = check_claim(CONDITION_CLAIM, None, PATIENT_ID)

    assert (check.resolved, check.subject_ok, check.status_ok, check.display_ok) == (
        False,
        False,
        False,
        False,
    )


def test_a_resource_that_belongs_to_another_patient_is_a_subject_mismatch():
    other = _condition(subject={"reference": "Patient/2000"})

    check = check_claim(CONDITION_CLAIM, other, PATIENT_ID)

    assert (check.resolved, check.subject_ok, check.status_ok) == (True, False, True)


def test_a_reference_that_is_not_a_patient_reference_is_a_subject_mismatch():
    unresolved = _condition(subject={"reference": "urn:uuid:b98e4960-beeb-41a3-9400-3e07ece715d0"})

    assert check_claim(CONDITION_CLAIM, unresolved, PATIENT_ID).subject_ok is False


def test_a_resource_without_a_subject_is_a_subject_mismatch():
    no_subject = {k: v for k, v in _condition().items() if k != "subject"}

    assert check_claim(CONDITION_CLAIM, no_subject, PATIENT_ID).subject_ok is False


def test_a_status_that_changed_since_the_packet_was_built_is_a_status_mismatch():
    resolved = _condition(clinicalStatus={"coding": [{"code": "resolved"}]})

    check = check_claim(CONDITION_CLAIM, resolved, PATIENT_ID)

    assert (check.resolved, check.subject_ok, check.status_ok) == (True, True, False)


def test_a_medication_is_checked_through_its_subject_and_its_status():
    claim = Claim("medication", "MedicationRequest/m1", "on-hold", "Warfarin")
    resource = {
        "resourceType": "MedicationRequest",
        "id": "m1",
        "status": "on-hold",
        "medicationCodeableConcept": {"text": "Warfarin"},
        "subject": {"reference": "Patient/1000"},
    }

    assert check_claim(claim, resource, PATIENT_ID) == SourceCheck(claim, True, True, True, True)


def test_an_allergy_is_checked_through_its_patient_field_not_subject():
    claim = Claim("allergy", "AllergyIntolerance/a1", "active", "Latex")
    resource = {
        "resourceType": "AllergyIntolerance",
        "id": "a1",
        "code": {"text": "Latex"},
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "patient": {"reference": "Patient/1000"},
    }

    assert check_claim(claim, resource, PATIENT_ID) == SourceCheck(claim, True, True, True, True)


def test_an_allergy_the_packet_says_has_no_status_must_really_have_none():
    claim = Claim("allergy", "AllergyIntolerance/a2", None, "Dust")
    without_status = {
        "resourceType": "AllergyIntolerance",
        "id": "a2",
        "patient": {"reference": "Patient/1000"},
    }
    with_status = {**without_status, "clinicalStatus": {"coding": [{"code": "active"}]}}

    assert check_claim(claim, without_status, PATIENT_ID).status_ok is True
    assert check_claim(claim, with_status, PATIENT_ID).status_ok is False


def test_the_patient_claim_is_a_subject_match_by_id_and_has_no_status_to_check():
    claim = Claim("patient", "Patient/1000", None, "")

    ok = check_claim(claim, {"resourceType": "Patient", "id": "1000"}, PATIENT_ID)
    other = check_claim(claim, {"resourceType": "Patient", "id": "999"}, PATIENT_ID)

    assert (ok.resolved, ok.subject_ok, ok.status_ok, ok.display_ok) == (True, True, None, None)
    assert other.subject_ok is False


def test_a_server_that_returns_a_different_resource_than_the_one_asked_for_does_not_resolve():
    # (AI) Claude Code's addition: "resolved" means the very resource named in the source.
    wrong_id = _condition(id="c9")
    wrong_type = {"resourceType": "Observation", "id": "c1"}

    assert check_claim(CONDITION_CLAIM, wrong_id, PATIENT_ID).resolved is False
    assert check_claim(CONDITION_CLAIM, wrong_type, PATIENT_ID).resolved is False


# ---- percentiles


def test_percentiles_use_the_nearest_rank_so_they_are_always_a_value_that_happened():
    values = list(range(1, 21))  # 1..20

    assert percentile(values, 50) == 10
    assert percentile(values, 95) == 19
    assert percentile(values, 100) == 20
    assert percentile([5, 1, 3], 50) == 3  # unsorted input


def test_a_percentile_of_nothing_is_nothing():
    assert percentile([], 50) is None


def test_a_percentile_of_one_value_is_that_value():
    assert percentile([7], 95) == 7


# ---- sampling patients for the batch


FILES = [f"Name{i}_Surname{i}_{i:08x}-0000-0000-0000-000000000000.json" for i in range(40)]


def test_the_same_seed_picks_the_same_patients_whatever_order_the_files_come_in():
    first = sample_uuids(FILES, 10, seed=1)
    second = sample_uuids(list(reversed(FILES)), 10, seed=1)

    assert first == second
    assert len(set(first)) == 10  # no patient twice


def test_a_different_seed_picks_different_patients():
    assert sample_uuids(FILES, 10, seed=1) != sample_uuids(FILES, 10, seed=2)


def test_asking_for_more_than_exist_returns_everyone_once():
    assert len(sample_uuids(FILES, 500, seed=1)) == 40


def test_sampled_ids_are_the_identifiers_from_the_filenames():
    (chosen,) = sample_uuids(FILES[:1], 1, seed=1)

    assert chosen == "00000000-0000-0000-0000-000000000000"


# ---- adding up a batch


def _result(
    uuid="u",
    *,
    checks=(),
    status="generated",
    reason=None,
    wall_ms=1000,
    http=200,
    counts=(1, 1, 1),
    excluded=(0, 0, 0),
    text_violation=None,
    flags=(),
    accounts=(),
    cached_before=False,
    problem=None,
) -> PatientResult:
    return PatientResult(
        uuid=uuid,
        http_status=http,
        summary_status=status if http == 200 else None,
        summary_reason=reason,
        counts=counts,
        excluded=excluded,
        checks=list(checks),
        wall_ms=wall_ms,
        text_violation=text_violation,
        flags=list(flags),
        accounts=list(accounts),
        cached_before=cached_before,
        problem=problem,
    )


def _check(kind="condition", *, resolved=True, subject=True, status=True) -> SourceCheck:
    return SourceCheck(Claim(kind, f"{kind}/1", "active", ""), resolved, subject, status)


def test_source_rates_are_counted_over_every_source_checked():
    results = [
        _result(checks=[_check(), _check(), _check(resolved=False, subject=False, status=False)]),
        _result(checks=[_check(), _check(subject=False)]),
    ]

    batch = summarize_batch(results)

    assert batch.sources_checked == 5
    assert batch.resolved == 4
    assert batch.subject_ok == 3
    assert batch.status_ok == 4  # a wrong subject can still carry the right status


def test_the_patient_claim_has_no_status_so_it_is_left_out_of_the_status_rate():
    patient = SourceCheck(Claim("patient", "Patient/1", None, ""), True, True, None)

    batch = summarize_batch([_result(checks=[patient, _check()])])

    assert batch.sources_checked == 2
    assert batch.status_checked == 1  # only the condition
    assert batch.status_ok == 1


def test_summaries_are_counted_by_outcome_and_reason():
    results = [
        _result(),
        _result(),
        _result(status="unavailable", reason="invalid_output"),
        _result(status="unavailable", reason="timeout"),
        _result(http=404),
    ]

    batch = summarize_batch(results)

    assert batch.patients == 5
    assert batch.not_answered == 1  # the 404: never got as far as a summary
    assert batch.generated == 2
    assert batch.unavailable_by_reason == {"invalid_output": 1, "timeout": 1}


def test_schema_valid_means_the_model_answered_in_the_agreed_shape():
    # (AI) Claude Code's reading of "schema-valid": every summary the model got as far as answering,
    # minus the ones whose answer was not exactly {"summary": <string>}. A timeout or a model that
    # was not running never produced an answer, so they say nothing about the model's output.
    results = [_result()] * 18 + [
        _result(status="unavailable", reason="invalid_output"),
        _result(status="unavailable", reason="policy_violation"),
        _result(status="unavailable", reason="timeout"),
    ]

    batch = summarize_batch(results)

    assert batch.answered_by_model == 20  # everything except the timeout
    assert batch.schema_valid == 19  # everything except the invalid_output
    assert batch.schema_valid_rate == pytest.approx(0.95)


def test_latency_is_measured_over_summaries_a_reviewer_actually_got():
    results = [_result(wall_ms=ms) for ms in (1000, 2000, 3000, 4000)]
    results.append(_result(status="unavailable", reason="timeout", wall_ms=45000))

    batch = summarize_batch(results)

    assert batch.latency_p50_ms == 2000
    assert batch.latency_p95_ms == 4000


def test_how_often_each_list_is_empty_and_how_many_records_were_excluded():
    results = [
        _result(counts=(0, 0, 0), excluded=(0, 0, 0)),
        _result(counts=(3, 0, 2), excluded=(1, 10, 0)),
        _result(counts=(5, 4, 0), excluded=(2, 0, 0)),
        _result(counts=(2, 2, 2), excluded=(1, 40, 1)),
    ]

    batch = summarize_batch(results)

    assert batch.empty_rate == {"conditions": 0.25, "medications": 0.5, "allergies": 0.5}
    assert batch.excluded_max == {"conditions": 2, "medications": 40, "allergies": 1}
    assert batch.excluded_p50 == {"conditions": 1, "medications": 0, "allergies": 0}


def test_an_empty_batch_summarizes_to_zeros_not_a_crash():
    batch = summarize_batch([])

    assert batch.patients == 0
    assert batch.latency_p50_ms is None
    assert batch.schema_valid_rate is None


def test_returned_summaries_that_break_a_word_rule_are_counted_by_rule():
    # (AI) An independent re-check of the text the API returned. The service already refuses to
    # return such text, so anything counted here would mean that guard failed.
    results = [
        _result(),
        _result(text_violation="determination_language"),
        _result(text_violation="determination_language"),
        _result(text_violation="control_claim"),
        _result(status="unavailable", reason="timeout"),
    ]

    batch = summarize_batch(results)

    assert batch.text_violations == {"determination_language": 2, "control_claim": 1}


def test_a_patient_with_no_packet_says_nothing_about_how_often_lists_are_empty():
    results = [_result(counts=(0, 0, 0)), _result(http=404)]

    batch = summarize_batch(results)

    assert batch.empty_rate == {"conditions": 1.0, "medications": 1.0, "allergies": 1.0}


# ---- does the fact say what the record says?


@pytest.mark.parametrize(
    ("resource", "label", "expected"),
    [
        (
            {"code": {"text": "Anemia", "coding": [{"display": "Anemia (disorder)"}]}},
            "Anemia",
            True,
        ),
        (
            {"code": {"coding": [{"display": "Anemia (disorder)", "code": "271737000"}]}},
            "Anemia (disorder)",
            True,
        ),
        ({"code": {"coding": [{"code": "271737000"}]}}, "271737000", True),  # only a code on file
        ({"code": {"text": "Anemia"}}, "Diabetes", False),  # a different condition entirely
        ({"code": {"text": "Anemia"}}, "anemia", False),  # exact text, not "close enough"
        ({}, "unknown", True),  # the record has no name at all
        ({}, "Diabetes", False),
        ({"medicationCodeableConcept": {"text": "Warfarin 5 MG"}}, "Warfarin 5 MG", True),
        ({"medicationReference": {"display": "Warfarin"}}, "Warfarin", True),
        ({"medicationReference": {"reference": "Medication/9"}}, "Medication/9", True),
        ({}, "unknown medication", True),
    ],
)
def test_a_fact_must_use_a_name_the_record_itself_carries(resource, label, expected):
    kind = (
        "medication" if label.startswith(("Warfarin", "Medication", "unknown med")) else "condition"
    )
    type_name = "MedicationRequest" if kind == "medication" else "Condition"
    claim = Claim(kind, f"{type_name}/x", "active", label)
    resource = {
        "resourceType": type_name,
        "id": "x",
        "status": "active",
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "subject": {"reference": "Patient/1000"},
        **resource,
    }

    assert check_claim(claim, resource, PATIENT_ID).display_ok is expected


# ---- does the packet account for every record HAPI holds?


def _accounted(*, shown=(2, 2, 5), excluded=(3, 2, 1), invalid=(0, 0, 0), missing=()) -> dict:
    return {
        "conditions": [{}] * shown[0],
        "medications": [{}] * shown[1],
        "allergies": [{}] * shown[2],
        "meta": {
            "excluded_counts": dict(zip(SECTIONS, excluded, strict=True)),
            "invalid_counts": dict(zip(SECTIONS, invalid, strict=True)),
        },
        "missing": list(missing),
    }


def test_every_record_is_accounted_for_when_shown_plus_left_out_equals_what_hapi_holds():
    accounts = account_for(_accounted(), {"conditions": 5, "medications": 4, "allergies": 6})

    assert accounts == [
        SectionAccount("conditions", hapi_total=5, accounted=5),
        SectionAccount("medications", hapi_total=4, accounted=4),
        SectionAccount("allergies", hapi_total=6, accounted=6),
    ]
    assert all(account.ok for account in accounts)


def test_records_cut_by_the_list_cap_are_accounted_for_from_the_truncation_notice():
    packet = _accounted(
        shown=(25, 0, 0),
        excluded=(10, 0, 0),
        missing=[
            {
                "code": "truncated_section",
                "section": "conditions",
                "detail": "showing 25 of 40 included Condition resources, newest first",
            }
        ],
    )

    (conditions, *_) = account_for(packet, {"conditions": 50, "medications": 0, "allergies": 0})

    assert conditions.accounted == 50  # 25 shown + 15 cut + 10 excluded
    assert conditions.ok


def test_records_the_service_could_not_read_are_accounted_for_too():
    packet = _accounted(
        shown=(1, 0, 0),
        excluded=(0, 0, 0),
        missing=[
            {
                "code": "unparseable_resource",
                "section": "conditions",
                "detail": "3 Condition resources could not be read and were skipped",
            }
        ],
    )

    (conditions, *_) = account_for(packet, {"conditions": 4, "medications": 0, "allergies": 0})

    assert conditions.accounted == 4


def test_a_packet_that_shows_fewer_records_than_hapi_holds_is_caught():
    # The failure this exists for: a search that stopped after its first page.
    packet = _accounted(shown=(0, 10, 0), excluded=(0, 90, 0))

    accounts = account_for(packet, {"conditions": 0, "medications": 1275, "allergies": 0})

    medications = accounts[1]
    assert (medications.hapi_total, medications.accounted, medications.ok) == (1275, 100, False)


def test_a_packet_that_accounts_for_more_than_hapi_holds_is_caught_too():
    packet = _accounted(shown=(5, 0, 0), excluded=(0, 0, 0))

    (conditions, *_) = account_for(packet, {"conditions": 2, "medications": 0, "allergies": 0})

    assert conditions.ok is False


# ---- wording that suggests a problem (a person still decides)


@pytest.mark.parametrize(
    "text",
    [
        "The patient is taking metformin and lisinopril.",
        "She is currently taking insulin.",
        "He is being treated for hypertension.",
        "The patient currently has diabetes.",
        "They are receiving warfarin.",
    ],
)
def test_present_tense_claims_of_treatment_are_flagged_for_a_living_patient(text):
    assert wording_flags(text, deceased=False) == ["present_tense"]


@pytest.mark.parametrize(
    "text",
    [
        "The patient has recorded active anemia and prediabetes.",
        "Metformin is recorded as active.",
        "The record is on file for the patient.",
        "The patient was taking metformin.",
    ],
)
def test_wording_that_states_what_the_record_says_is_not_flagged(text):
    assert wording_flags(text, deceased=False) == []


def test_a_deceased_patients_summary_must_say_so():
    silent = "A 95-year-old male had heart failure and took several medications."

    assert wording_flags(silent, deceased=True) == ["deceased_not_stated"]


@pytest.mark.parametrize(
    "text",
    [
        "A 93-year-old male, now deceased, had Alzheimer's disease.",
        "The patient died at 14 and had seven recorded allergies.",
        "The record shows the patient passed away; conditions are the last recorded.",
    ],
)
def test_a_deceased_patient_described_as_deceased_is_not_flagged(text):
    assert wording_flags(text, deceased=True) == []


def test_a_deceased_patient_described_as_currently_treated_is_flagged_even_if_death_is_stated():
    text = "The patient is deceased and is currently taking warfarin."

    assert wording_flags(text, deceased=True) == ["deceased_present_tense"]


# ---- how sure a rate is


def test_a_rate_comes_with_the_range_it_could_really_be():
    low, high = wilson_interval(19, 20)

    assert low == pytest.approx(0.764, abs=0.005)
    assert high == pytest.approx(0.991, abs=0.005)


def test_a_perfect_rate_from_few_samples_is_not_claimed_to_be_certain():
    low, high = wilson_interval(10, 10)

    assert high == pytest.approx(1.0)
    assert low < 0.75  # ten out of ten only tells you it is probably above about 72%


def test_more_samples_narrow_the_range():
    small = wilson_interval(95, 100)
    smaller_still = wilson_interval(19, 20)

    assert (small[1] - small[0]) < (smaller_still[1] - smaller_still[0])


def test_a_rate_over_no_samples_has_no_range():
    assert wilson_interval(0, 0) is None


# ---- the batch report, extended


def test_summaries_remembered_from_an_earlier_run_do_not_count_as_latency():
    results = [
        _result(wall_ms=3000),
        _result(wall_ms=5000),
        # the service already had these: near-instant, and not the model's speed
        *[_result(wall_ms=1, cached_before=True) for _ in range(3)],
    ]

    batch = summarize_batch(results)

    assert batch.cached_before == 3
    assert batch.latency_p50_ms == 3000
    assert batch.latency_p95_ms == 5000


def test_a_summary_that_needed_a_second_request_still_counts_its_whole_wait():
    # The first request timed out and the second was answered from memory: that wait is real.
    results = [_result(wall_ms=52000, cached_before=False)]

    assert summarize_batch(results).latency_p50_ms == 52000


def test_wording_flags_are_counted_and_the_patients_are_listed_for_a_person_to_read():
    results = [
        _result("a"),
        _result("b", flags=["present_tense"]),
        _result("c", flags=["present_tense", "deceased_not_stated"]),
    ]

    batch = summarize_batch(results)

    assert batch.flag_counts == {"present_tense": 2, "deceased_not_stated": 1}
    assert batch.flagged == {"present_tense": ["b", "c"], "deceased_not_stated": ["c"]}


def test_completeness_and_display_are_counted_over_the_batch():
    good = [SectionAccount("conditions", 5, 5), SectionAccount("medications", 4, 4)]
    bad = [SectionAccount("conditions", 1275, 100)]
    matching = _check()
    wrong_text = SourceCheck(_check().claim, True, True, True, display_ok=False)
    results = [
        _result("a", accounts=good, checks=[SourceCheck(matching.claim, True, True, True, True)]),
        _result("b", accounts=bad, checks=[wrong_text]),
    ]

    batch = summarize_batch(results)

    assert (batch.completeness_checked, batch.completeness_ok) == (3, 2)
    assert (batch.display_checked, batch.display_ok) == (2, 1)
    assert batch.incomplete == ["b"]


def test_a_patient_the_script_could_not_finish_is_a_problem_not_a_pass():
    results = [_result(), _result("b", problem="HAPI returned 500 for Condition/1")]

    batch = summarize_batch(results)

    assert batch.problems == {"b": "HAPI returned 500 for Condition/1"}


def test_the_share_of_reviewers_who_got_a_summary_is_reported_next_to_the_schema_rate():
    results = [_result()] * 8 + [_result(status="unavailable", reason="timeout")] * 2
    results.append(_result(http=404))  # no packet at all: not part of "patients who got a packet"

    batch = summarize_batch(results)

    assert batch.usable_rate == pytest.approx(0.8)


def test_records_entered_in_error_are_accounted_for_too():
    packet = _accounted(shown=(1, 0, 0), excluded=(0, 0, 0), invalid=(2, 0, 0))

    (conditions, *_) = account_for(packet, {"conditions": 3, "medications": 0, "allergies": 0})

    assert conditions.accounted == 3


# ---- a long chart summarized as if the few conditions named were all of them


def test_naming_conditions_from_a_long_chart_without_saying_there_are_more_is_flagged():
    text = "The patient is recorded as active with sinusitis, kidney disease, and renal disease."

    assert wording_flags(text, deceased=False, conditions=24) == ["partial_list"]


@pytest.mark.parametrize(
    "text",
    [
        "The patient has recorded active conditions including sinusitis and kidney disease.",
        "Recorded conditions such as sinusitis and kidney disease.",
        "Sinusitis, kidney disease, among other conditions.",
        "The patient has several recorded conditions.",
        "Sinusitis and multiple other recorded conditions.",
        "Various conditions are recorded, from sinusitis to kidney disease.",
    ],
)
def test_saying_there_are_more_is_not_flagged(text):
    assert wording_flags(text, deceased=False, conditions=24) == []


def test_a_short_chart_is_not_flagged_for_naming_what_it_has():
    text = "The patient is recorded as active with seasonal allergic rhinitis and obesity."

    assert wording_flags(text, deceased=False, conditions=3) == []


def test_the_partial_list_flag_can_sit_alongside_the_deceased_flags():
    text = "The patient is deceased and is taking furosemide for heart failure and anemia."

    assert wording_flags(text, deceased=True, conditions=12) == [
        "deceased_present_tense",
        "partial_list",
    ]
