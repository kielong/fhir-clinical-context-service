# ORIGIN: H-spec — Kiel's decision: every answer must pass these checks before a reviewer sees it;
#   the exact things it may not say are invented facts, identifiers, determinations, control or
#   prognosis claims. Lines typed by Claude Code. The checks nobody specified are labeled AI where
#   they occur.
"""What the model may say. Anything that fails a check is thrown away, never shown.

These are deliberately narrow, deterministic checks on words and on a few clear falsehoods. They
are not a general fact-checker: other unfaithful wording is what the evaluation measures.
"""

import re
from enum import StrEnum

from ..packet.models import ClinicalContextPacket

# ORIGIN: H-spec — Kiel's decision: a summary is capped by word count, not by sentences or
#   characters. A reviewer scanning a chart can read a paragraph; what matters is that it stays
#   short enough to scan and that every other check below still holds. Lines typed by Claude Code.
MAX_SUMMARY_WORDS = 200


class Violation(StrEnum):
    """The rule a rejected answer broke. The value is what the retry prompt is keyed on."""

    INVALID_JSON = "invalid_json"
    EMPTY = "empty"
    TOO_LONG = "too_long"
    CONTAINS_IDENTIFIER = "contains_identifier"
    DETERMINATION_LANGUAGE = "determination_language"
    CONTROL_CLAIM = "control_claim"
    CONTRADICTS_FACTS = "contradicts_facts"
    UNSUPPORTED_NUMBER = "unsupported_number"
    DECEASED_NOT_STATED = "deceased_not_stated"
    DECEASED_PRESENT_TENSE = "deceased_present_tense"


# What the retry prompt tells the model it did wrong.
REJECTION_REASONS = {
    Violation.INVALID_JSON: "it was not valid JSON with a single string field named summary",
    Violation.EMPTY: "it was empty",
    Violation.TOO_LONG: f"it was longer than {MAX_SUMMARY_WORDS} words",
    Violation.CONTAINS_IDENTIFIER: "it contained an identifier",
    Violation.DETERMINATION_LANGUAGE: (
        "it used determination language (approve, deny, authorize, medically necessary, eligible)"
    ),
    Violation.CONTROL_CLAIM: (
        "it claimed a condition was controlled, stable, improving, or worsening"
    ),
    Violation.CONTRADICTS_FACTS: (
        "it said none were recorded for something the record lists, which contradicted the "
        "listed facts"
    ),
    Violation.UNSUPPORTED_NUMBER: (
        "it used a number or date that is not in the listed facts; use only numbers that appear "
        "in them"
    ),
    Violation.DECEASED_NOT_STATED: (
        "it did not say the patient is deceased; the patient is deceased, so the first sentence "
        'must contain the word "deceased"'
    ),
    Violation.DECEASED_PRESENT_TENSE: (
        "it described a deceased patient as currently on treatment; use the past tense for "
        "conditions and medications"
    ),
}

_IDENTIFIER = re.compile(
    r"\b(?:Patient|Condition|MedicationRequest|AllergyIntolerance)/\S+"
    r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
# ORIGIN: AI — edge case added by Claude Code: word boundaries, so "denies chest pain" (a symptom)
#   and "stability" are allowed while "denied" and "stable" are not.
_DETERMINATION = re.compile(
    r"approv|\bden(?:y|ied|ial)\b|authori[sz]|medically necessary|medical necessity|eligib",
    re.IGNORECASE,
)
_CONTROL = re.compile(
    r"well[- ]controlled|poorly controlled|uncontrolled|\bstable\b|\bimproving\b|\bworsening\b",
    re.IGNORECASE,
)


def validate_summary(text: str) -> Violation | None:
    """None if the summary may be shown, else the first word-level rule it breaks."""
    text = text.strip()
    if not text:
        return Violation.EMPTY
    if len(text.split()) > MAX_SUMMARY_WORDS:
        return Violation.TOO_LONG
    if _IDENTIFIER.search(text):
        return Violation.CONTAINS_IDENTIFIER
    if _DETERMINATION.search(text):
        return Violation.DETERMINATION_LANGUAGE
    if _CONTROL.search(text):
        return Violation.CONTROL_CLAIM
    return None


# ORIGIN: AI — added by Claude Code after a live run: a 3B model told a reviewer "no reported
#   allergies" for a patient whose record lists five. validate_summary looks only at the words, so
#   it could not catch that. This is a deliberately narrow, deterministic check for the clearest
#   possible falsehood: saying none of something is recorded when the packet lists some. A probe
#   later showed the first version caught 3 of 9 ways of saying it, so it now covers the common
#   families: "no / without / lacks / free of ...", "not taking / not on any ...", "... are not
#   recorded", "... : none", and "nothing recorded for ...". Each is limited to one clause (words
#   only, so a comma or a full stop ends it) and to a fixed list of filler words, so "no active
#   conditions, but a shellfish allergy is recorded" is not read as "no allergies". It is not a
#   general fact-checker; other unfaithful wording is what the evaluation measures.
_NOUNS = {
    "allergies": r"allerg(?:y|ies|ic|ens?)",
    # "no known drug allergies" is about allergies, not medications
    "medications": r"(?:medications?|prescriptions?|drugs?|medicines?)(?!\s+allerg)",
    "conditions": r"(?:conditions?|diagnos[ei]s|problems?|illness(?:es)?|diseases?)",
}
_FILLER = (
    r"(?:(?:recorded|reported|known|documented|listed|noted|identified|confirmed|active|current|"
    r"currently|ongoing|chronic|prior|past|significant|other|further|additional|medical|food|"
    r"drug|environmental|seasonal|on-hold|on|hold|history|of|or|and|nor|"
    r"allergies|allergy|conditions?|diagnos[ei]s|problems?|medications?|prescriptions?)\s+)*"
)
_NEGATOR = (
    r"\b(?:no|without|zero|lacks?|lacking|free\s+of|absence\s+of"
    r"|(?:not|never)\s+(?:currently\s+|presently\s+|actively\s+)?(?:on|taking|prescribed|using)"
    r"|(?:does|do|did|has|have|had)(?:\s+not|n't)\s+(?:currently\s+)?"
    r"(?:have|take|use|been\s+prescribed))\s+(?:any\s+)?"
)
_NOT_RECORDED = (
    r"(?:\s+(?:are|is|were|was|have\s+been|has\s+been))?\s+(?:not|never)\s+(?:been\s+)?"
    r"(?:recorded|listed|reported|documented|noted|known|present|available|on\s+file)"
    r"|(?:\s*:\s*|\s+(?:are\s+|is\s+)?)(?:none|nil|absent|missing)\b"
)
_NONE_FOR = (
    r"\b(?:none|nothing)\s+(?:is\s+|are\s+)?(?:recorded|listed|reported|documented|noted|on\s+file)"
    r"\s+(?:for|about|regarding|on|of|in)\s+(?:the\s+|any\s+)?"
)


def _claims_none(noun: str) -> re.Pattern[str]:
    return re.compile(
        rf"{_NEGATOR}{_FILLER}{noun}|{noun}(?:{_NOT_RECORDED})|{_NONE_FOR}{_FILLER}{noun}",
        re.IGNORECASE,
    )


_CLAIMS_NONE = {section: _claims_none(noun) for section, noun in _NOUNS.items()}
_NOT_ALLERGIC = re.compile(r"\bnot\s+allergic\s+to\s+(?:any|anything|everything)\b", re.IGNORECASE)


def contradicts_facts(text: str, packet: ClinicalContextPacket) -> Violation | None:
    """CONTRADICTS_FACTS if the text says none are recorded for a list the packet fills."""
    listed = {
        "allergies": packet.allergies,
        "medications": packet.medications,
        "conditions": packet.conditions,
    }
    for section, pattern in _CLAIMS_NONE.items():
        if listed[section] and pattern.search(text):
            return Violation.CONTRADICTS_FACTS
    if packet.allergies and _NOT_ALLERGIC.search(text):
        return Violation.CONTRADICTS_FACTS
    return None


# ORIGIN: AI — added by Claude Code after a probe: a model can write a date, a dose or an age that
#   nothing in the record supports, and no word check sees it. The prompt contains every number the
#   model was given (the age, doses, counts of excluded records), and the length of each list is
#   allowed too, so "3 recorded conditions" is fine when three are listed. Any other digits are
#   invented. Spelled-out numbers ("three") are not checked: "one" is too common a word to police.
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def unsupported_numbers(
    text: str, packet: ClinicalContextPacket, user_prompt: str
) -> Violation | None:
    """UNSUPPORTED_NUMBER if a number is neither in the prompt nor the length of a list."""
    allowed = set(_NUMBER.findall(user_prompt))
    allowed.update(
        str(len(items)) for items in (packet.conditions, packet.medications, packet.allergies)
    )
    if any(number not in allowed for number in _NUMBER.findall(text)):
        return Violation.UNSUPPORTED_NUMBER
    return None


# ORIGIN: AI — added by Claude Code after the ten-patient evaluation: three of the four deceased
#   patients were summarized without ever saying they had died (one as "is taking medications"),
#   although the prompt asks for it. A model cannot be trusted to follow that, so it is checked.
#   For a deceased patient the summary must say so, and must not say they are currently on
#   treatment. A living patient is held to neither rule.
_SAYS_DECEASED = re.compile(r"\b(?:deceased|died|dead|death|passed\s+away)\b", re.IGNORECASE)
_CURRENT_TREATMENT = re.compile(
    r"\b(?:is|are)\s+(?:currently\s+|now\s+)?(?:taking|being\s+treated|receiving|prescribed)\b"
    r"|\bcurrently\s+(?:has|have|takes|taking|on|receiving)\b",
    re.IGNORECASE,
)


def deceased_violation(text: str, packet: ClinicalContextPacket) -> Violation | None:
    if not packet.patient.deceased:
        return None
    if not _SAYS_DECEASED.search(text):
        return Violation.DECEASED_NOT_STATED
    if _CURRENT_TREATMENT.search(text):
        return Violation.DECEASED_PRESENT_TENSE
    return None


def check_summary(text: str, packet: ClinicalContextPacket, user_prompt: str) -> Violation | None:
    """Every check an answer must pass, cheapest first. None means it may be shown."""
    return (
        validate_summary(text)
        or contradicts_facts(text, packet)
        or unsupported_numbers(text, packet, user_prompt)
        or deceased_violation(text, packet)
    )
