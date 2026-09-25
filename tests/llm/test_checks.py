# ORIGIN: AI — test cases typed by Claude Code from the agreed summarizer policy, reviewed by Kiel.
"""What the model may say: the checks every answer must pass before a reviewer sees it."""

import pytest

import synthetic as syn
from clinical_context.llm.checks import (
    MAX_SUMMARY_WORDS,
    REJECTION_REASONS,
    Violation,
    check_summary,
    contradicts_facts,
    deceased_violation,
    unsupported_numbers,
    validate_summary,
)
from clinical_context.llm.prompt import build_prompt
from ollama_mocks import GOOD, NEUTRAL
from packets import packet_from, real_packet


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (GOOD, None),
        ("One short sentence.", None),
        ("", "empty"),
        ("   ", "empty"),
        ("word " * (MAX_SUMMARY_WORDS + 1), "too_long"),
        ("word " * MAX_SUMMARY_WORDS, None),  # the limit itself is allowed
        ("First sentence. Second sentence. Third sentence.", None),  # sentences are not counted
        ("See Condition/123 for details.", "contains_identifier"),
        ("Recorded under MedicationRequest/abc.", "contains_identifier"),
        ("Patient 2fa15bc7-8866-461a-9000-f739e425860a is recorded.", "contains_identifier"),
        ("Recommend approval of the request.", "determination_language"),
        ("The claim should be denied.", "determination_language"),
        ("Request denied.", "determination_language"),
        ("This is authorized care.", "determination_language"),
        ("Treatment is medically necessary.", "determination_language"),
        ("There is medical necessity here.", "determination_language"),
        ("The patient is eligible for coverage.", "determination_language"),
        ("Their diabetes is well controlled.", "control_claim"),
        ("Their diabetes is well-controlled.", "control_claim"),
        ("Blood pressure is poorly controlled.", "control_claim"),
        ("The condition is uncontrolled.", "control_claim"),
        ("The patient is stable.", "control_claim"),
        ("Symptoms are improving.", "control_claim"),
        ("Symptoms are worsening.", "control_claim"),
    ],
)
def test_validate_summary_table(text, expected):
    assert validate_summary(text) == expected


def test_a_summary_is_measured_in_words_not_characters_or_sentences():
    # Long in characters and in sentences, but well under the word limit: it may be shown.
    text = " ".join(
        ["Recorded active on metformin, with hypertension and chronic kidney disease."] * 8
    )

    assert len(text) > 400
    assert len(text.split()) < MAX_SUMMARY_WORDS
    assert validate_summary(text) is None


def test_the_word_limit_is_two_hundred_words():
    assert MAX_SUMMARY_WORDS == 200


def test_words_that_only_contain_a_forbidden_stem_inside_another_word_are_allowed():
    # "denies" is clinical language (a patient denies pain), and "stability" is not "stable".
    assert validate_summary("The record notes the patient denies chest pain.") is None


def _packet_with(*, conditions=(), medications=(), allergies=()):
    return packet_from(
        patient=syn.patient("1"),
        conditions=list(conditions),
        medications=list(medications),
        allergies=list(allergies),
    )


ALLERGIC = _packet_with(allergies=[syn.allergy("a1", display="Shellfish allergy")])
ON_MEDS = _packet_with(medications=[syn.medication("m1")])
HAS_CONDITIONS = _packet_with(conditions=[syn.condition("c1")])
EMPTY = _packet_with()


@pytest.mark.parametrize(
    ("packet", "text"),
    [
        # Found live: a 3B model told a reviewer "no reported allergies" for a patient with five.
        (ALLERGIC, "A 23-year-old male with obesity, with no reported allergies."),
        (ALLERGIC, "The patient has no recorded allergies."),
        (ALLERGIC, "There are no known allergies. Nothing else."),
        (ALLERGIC, "Gaps in the record include no active allergies."),
        (ALLERGIC, "Seen without any documented allergies."),
        (ON_MEDS, "There are no active or on-hold medications."),
        (ON_MEDS, "The patient has no recorded medications."),
        (HAS_CONDITIONS, "The chart has no recorded conditions."),
        (HAS_CONDITIONS, "There are no active diagnoses on file."),
    ],
)
def test_saying_none_is_recorded_when_the_record_lists_some_is_a_contradiction(packet, text):
    assert contradicts_facts(text, packet) == "contradicts_facts"


@pytest.mark.parametrize(
    ("packet", "text"),
    [
        (EMPTY, "The patient has no recorded allergies and no active medications."),  # true
        (ALLERGIC, "The patient has a recorded shellfish allergy."),
        (ALLERGIC, "There are no active conditions, but a shellfish allergy is recorded."),
        (ON_MEDS, "The patient has no recorded allergies."),  # this packet really has none
        (HAS_CONDITIONS, "Recorded active type 2 diabetes; no medications are recorded."),
        (ALLERGIC, "No further details are recorded."),
    ],
)
def test_true_statements_of_absence_and_unrelated_wording_are_allowed(packet, text):
    assert contradicts_facts(text, packet) is None


# ---- the ways a model says "none recorded": the old check caught 3 of 9 of these phrasings

MORE_ABSENCE_CLAIMS = [
    (ALLERGIC, "The patient has no known drug allergies."),
    (ALLERGIC, "The patient has no documented food or drug allergies."),
    (ALLERGIC, "Allergies are not recorded."),
    (ALLERGIC, "Allergies: none recorded."),
    (ALLERGIC, "None recorded for allergies."),
    (ALLERGIC, "Nothing is recorded for allergies."),
    (ALLERGIC, "The patient is not allergic to anything."),
    (ALLERGIC, "There is an absence of allergies."),
    (ALLERGIC, "The patient has no history of allergies."),
    (ALLERGIC, "A patient free of allergies."),
    (ON_MEDS, "The patient is not taking any medications."),
    (ON_MEDS, "The patient is not currently on any medications."),
    (ON_MEDS, "The patient does not take any medications."),
    (ON_MEDS, "The patient takes no medications."),
    (ON_MEDS, "Medications are not listed."),
    (ON_MEDS, "Medications were never recorded."),
    (ON_MEDS, "Medications: none."),
    (ON_MEDS, "The patient has no prescriptions on file."),
    (ON_MEDS, "There are no recorded conditions or medications."),
    (HAS_CONDITIONS, "There are no recorded conditions or medications."),
    (HAS_CONDITIONS, "The patient has no medical problems."),
    (HAS_CONDITIONS, "No chronic conditions are recorded."),
    (HAS_CONDITIONS, "Conditions: none."),
    (HAS_CONDITIONS, "The patient has no diagnoses."),
    (HAS_CONDITIONS, "There is nothing recorded about conditions."),
]


@pytest.mark.parametrize(("packet", "text"), MORE_ABSENCE_CLAIMS)
def test_every_common_way_of_saying_none_is_recorded_is_caught(packet, text):
    assert contradicts_facts(text, packet) == "contradicts_facts"


@pytest.mark.parametrize(
    ("packet", "text"),
    [
        # "no known drug allergies" is about allergies, not medications
        (ON_MEDS, "The patient has no known drug allergies."),
        (ON_MEDS, "There are no medication allergies on record."),
        (HAS_CONDITIONS, "The record lists no medications."),  # true for this packet
        (ALLERGIC, "The patient is allergic to shellfish."),
        (ALLERGIC, "Allergies are recorded as shellfish."),
        (ALLERGIC, "There are no medications or conditions, but a shellfish allergy is recorded."),
        (ALLERGIC, "There are no medications or conditions but a shellfish allergy is recorded."),
        (ALLERGIC, "The patient has a shellfish allergy and no medications."),
        (HAS_CONDITIONS, "Conditions are recorded as active."),
        (ON_MEDS, "Allergies are not recorded."),  # true: this packet lists no allergies
        (EMPTY, "Allergies: none recorded. Medications: none. Conditions: none."),
    ],
)
def test_true_or_unrelated_absence_wording_is_still_allowed(packet, text):
    assert contradicts_facts(text, packet) is None


# ---- numbers and dates the record never gave


def test_a_number_that_is_in_the_prompt_is_allowed():
    packet = real_packet("Aaron697_Brekke496")
    _, user = build_prompt(packet)

    assert unsupported_numbers("A 73-year-old male with obesity (BMI 30+).", packet, user) is None


def test_the_count_of_listed_items_is_allowed_even_though_it_is_not_written_in_the_prompt():
    packet = _packet_with(conditions=[syn.condition(f"c{i}") for i in range(3)])
    _, user = build_prompt(packet)

    assert unsupported_numbers("The patient has 3 recorded conditions.", packet, user) is None
    assert unsupported_numbers("The patient has 4 recorded conditions.", packet, user) == (
        "unsupported_number"
    )


@pytest.mark.parametrize(
    "text",
    [
        "A 57-year-old male with anemia.",  # a different age
        "Diagnosed with anemia in 2015.",  # a date the record never gave
        "Recorded active since 2019-03-01.",
        "Takes metformin 850 mg daily.",  # a dose the record did not list
    ],
)
def test_an_invented_number_or_date_is_rejected(text):
    packet = real_packet("Aaron697_Brekke496")
    _, user = build_prompt(packet)

    assert unsupported_numbers(text, packet, user) == "unsupported_number"


def test_a_dose_that_is_in_the_record_is_allowed():
    packet = _packet_with(medications=[syn.medication("m1", drug="Metformin 500 MG Oral Tablet")])
    _, user = build_prompt(packet)

    assert unsupported_numbers("Recorded on metformin 500 mg.", packet, user) is None


def test_text_without_numbers_has_no_unsupported_numbers():
    packet = real_packet("Aaron697_Brekke496")
    _, user = build_prompt(packet)

    assert unsupported_numbers(GOOD, packet, user) is None


# ---- all the checks, in one place


def test_check_summary_runs_the_word_checks_then_the_record_checks():
    packet = ALLERGIC
    _, user = build_prompt(packet)

    assert check_summary(NEUTRAL, packet, user) is None
    assert check_summary("", packet, user) == Violation.EMPTY
    assert check_summary("Approval is recommended.", packet, user) == (
        Violation.DETERMINATION_LANGUAGE
    )
    assert check_summary("No allergies are recorded.", packet, user) == (
        Violation.CONTRADICTS_FACTS
    )
    assert check_summary("A 99-year-old with allergies.", packet, user) == (
        Violation.UNSUPPORTED_NUMBER
    )


def test_every_violation_has_a_reason_the_retry_can_give_the_model():
    assert set(REJECTION_REASONS) == set(Violation)


# ---- a deceased patient must be described as deceased, and not as currently on treatment
#      (found by the evaluation: three of four deceased patients were summarized as if living)

DECEASED_PATIENT = real_packet("Floyd420_Jerde200")  # deceased at 95
LIVING_PATIENT = real_packet("Aaron697_Brekke496")


@pytest.mark.parametrize(
    "text",
    [
        "A 95-year-old male had chronic heart failure and took several medications.",
        "The patient is recorded as active with heart failure and Alzheimer's disease.",
        "The patient's last recorded medications include furosemide and insulin.",
    ],
)
def test_a_deceased_patients_summary_that_never_says_so_is_rejected(text):
    assert deceased_violation(text, DECEASED_PATIENT) == Violation.DECEASED_NOT_STATED


@pytest.mark.parametrize(
    "text",
    [
        "A 95-year-old male, now deceased, had recorded heart failure.",
        "The patient died at age 95; the last recorded conditions include heart failure.",
        "The record shows the patient passed away and lists heart failure as last recorded.",
        "DECEASED at 95, the patient had recorded heart failure.",
    ],
)
def test_a_deceased_patient_described_as_deceased_is_accepted(text):
    assert deceased_violation(text, DECEASED_PATIENT) is None


@pytest.mark.parametrize(
    "text",
    [
        "The patient is deceased and is taking furosemide.",
        "The patient died in 2017 but is currently on insulin.",
        "A deceased patient who is being treated for heart failure.",
        "The patient, deceased, currently has diabetes.",
        "The patient is deceased and is receiving warfarin.",
    ],
)
def test_a_deceased_patient_described_as_currently_on_treatment_is_rejected(text):
    assert deceased_violation(text, DECEASED_PATIENT) == Violation.DECEASED_PRESENT_TENSE


def test_a_deceased_patient_described_in_the_past_tense_is_accepted():
    text = "The patient is deceased; the last recorded medications were furosemide and insulin."

    assert deceased_violation(text, DECEASED_PATIENT) is None


def test_a_living_patient_is_not_held_to_either_rule():
    # Present tense for a living patient is what the record says; nothing needs to say "deceased".
    text = "The patient is taking metformin and has recorded active anemia."

    assert deceased_violation(text, LIVING_PATIENT) is None


def test_check_summary_applies_the_deceased_rules_after_the_others():
    _, user = build_prompt(DECEASED_PATIENT)

    assert check_summary(
        "A 95-year-old male had recorded heart failure.", DECEASED_PATIENT, user
    ) == (Violation.DECEASED_NOT_STATED)
    assert check_summary("Approval is recommended.", DECEASED_PATIENT, user) == (
        Violation.DETERMINATION_LANGUAGE  # the older rules still come first
    )
    assert (
        check_summary(
            "A 95-year-old male, now deceased, had recorded heart failure.", DECEASED_PATIENT, user
        )
        is None
    )
