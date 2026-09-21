# ORIGIN: H-spec — Kiel's decision: the model never sees a source, an id, or the patient's name; it
#   is given plain lines derived from the packet, and gaps go to it in plain words with how many
#   records were excluded as no longer active. The rules it is told are the exact things it is
#   forbidden to say. Lines typed by Claude Code; the exact wording is Claude Code's.
"""What the model is told: a fixed rulebook (system) and the packet's facts as plain lines (user).

The prompt is a pure function of the packet: no ids, no clock, nothing that varies between runs.
That is half of "same packet, same words"; the other half is in ollama.py (pinned decoding) and
cache.py (the finished summary is remembered).
"""

import re
import unicodedata

from ..packet.models import ClinicalContextPacket
from .checks import REJECTION_REASONS

SYSTEM_PROMPT = """\
You write a two-sentence scan summary of one patient's chart for a utilization-management reviewer.

Use only the facts listed in the user message.

Rules:
1. Write exactly two plain sentences.
2. Never add a diagnosis, medication, or allergy that is not listed.
3. Never write an identifier, a resource name, or the patient's name.
4. Never say care should be approved, denied, or authorized, that anything is medically necessary, \
or that the patient is eligible for anything.
5. Never say a condition is controlled, stable, improving, or worsening, and never predict an \
outcome.
6. Statuses are what the record says. Write "recorded as active", never "currently has".
7. If the patient is deceased, your first sentence must contain the word "deceased" (for example \
"A 93-year-old man, now deceased, had ..."), and everything else must be in the past tense. Never \
describe a deceased patient as currently on treatment.
8. If a list says none, you may say none is recorded. Never say more than the lists say.
9. If a list has more items than you can name in two sentences, name only the first few and say \
"including"; never present a partial list as the whole list.

Answer only with JSON of the form {"summary": "<the two sentences>"}."""

# The trailing "(disorder)" style tags SNOMED puts on display names. Stripped from the prompt only;
# the packet keeps the exact display. Only these known tags: a meaningful parenthetical such as
# "Hypertension (high blood pressure)" must survive.
_SEMANTIC_TAGS = (
    "disorder",
    "finding",
    "situation",
    "morphologic abnormality",
    "procedure",
    "regime/therapy",
    "event",
    "observable entity",
    "substance",
    "product",
    "medicinal product",
    "clinical drug",
    "body structure",
    "qualifier value",
    "environment",
    "social concept",
)
_TAG = re.compile(r"\s*\((?:" + "|".join(re.escape(tag) for tag in _SEMANTIC_TAGS) + r")\)\s*$")


def strip_semantic_tag(display: str) -> str:
    return _TAG.sub("", display)


MAX_LABEL_CHARS = 160


# ORIGIN: AI — added by Claude Code after a probe: a display name is text from the record, and a
#   newline in one could start a line of its own in the prompt, such as a fake "Rules:" section. So
#   every label is flattened to one plain line before it is placed. This stops the structure being
#   forged; it does not make a label trustworthy, which is why every answer is still checked.
def clean_label(display: str) -> str:
    """One plain line: control, invisible and line-break characters become spaces, runs of
    whitespace collapse, the SNOMED tag is dropped, and the length is capped."""
    flattened = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in display)
    label = strip_semantic_tag(" ".join(flattened.split()))
    return label[:MAX_LABEL_CHARS].rstrip()


_ADJECTIVE = {"conditions": "condition", "medications": "medication", "allergies": "allergy"}
_NONE_ON_FILE = {
    "conditions": "No conditions are recorded",
    "medications": "No medications are recorded",
    "allergies": "No allergies are recorded",
}
_NONE_ACTIVE = {
    "conditions": "No active conditions are recorded",
    "medications": "No active or on-hold medications are recorded",
    "allergies": "No active allergies are recorded",
}


# ORIGIN: AI — added by Claude Code after the ten-patient evaluation: the rule in the system prompt
#   sits far from the facts, and on a long chart a 4B model ignored it even when told again. The
#   last thing a small model reads is what it follows, so the reminder goes at the end.
_DECEASED_REMINDER = (
    'Reminder: the patient is deceased, so your first sentence must contain the word "deceased", '
    "and everything else must be in the past tense."
)


def _patient_line(packet: ClinicalContextPacket) -> str:
    patient = packet.patient
    gender = patient.gender if patient.gender in ("male", "female") else None
    age = patient.age_years
    if patient.deceased:
        parts = [gender] if gender else []
        parts.append(f"deceased at age {age}" if age is not None else "deceased")
        return (
            f"Patient: {', '.join(parts)}. "
            "Statuses below are the last recorded, not current treatment."
        )
    if age is None:
        return f"Patient: age unknown, {gender}." if gender else "Patient: age unknown."
    return f"Patient: {age}-year-old {gender or 'patient'}."


def _bullets(labels: list[str]) -> list[str]:
    return [f"- {label}" for label in labels] or ["- none"]


def _gap_lines(packet: ClinicalContextPacket) -> list[str]:
    excluded = packet.meta.excluded_counts.model_dump()
    lines = []
    for gap in packet.missing:
        section = gap.section
        if gap.code == "empty_section":
            count = excluded[section]
            if count == 0:
                lines.append(_NONE_ON_FILE[section])
                continue
            others = "1 other is" if count == 1 else f"{count} others are"
            lines.append(f"{_NONE_ACTIVE[section]} ({others} recorded as no longer active)")
        elif gap.code == "truncated_section":
            lines.append(f"Only the most recent {section} are listed")
        elif gap.code == "unparseable_resource":
            lines.append(f"Some {_ADJECTIVE[section]} records could not be read")
        # patient_deceased is already stated in the patient line
    return [f"- {line}" for line in lines]


def build_prompt(packet: ClinicalContextPacket) -> tuple[str, str]:
    """(system, user). Built only from the packet's facts: no ids, no names, no timestamps."""
    conditions = [clean_label(c.display) for c in packet.conditions]
    medications = [
        clean_label(m.display) + (" (on hold)" if m.status == "on-hold" else "")
        for m in packet.medications
    ]
    allergies = [
        clean_label(a.display) + (f" (criticality: {a.criticality})" if a.criticality else "")
        for a in packet.allergies
    ]
    lines = [
        _patient_line(packet),
        "Conditions (recorded active):",
        *_bullets(conditions),
        "Medications (recorded active or on hold):",
        *_bullets(medications),
        "Allergies:",
        *_bullets(allergies),
    ]
    gaps = _gap_lines(packet)
    if gaps:
        lines += ["Gaps in the record:", *gaps]
    if packet.patient.deceased:
        lines.append(_DECEASED_REMINDER)
    return SYSTEM_PROMPT, "\n".join(lines)


def with_correction(user: str, problem: str) -> str:
    """The retry prompt: same facts, plus what was wrong (a repeat of the same prompt at
    temperature 0 would just produce the same answer). The rejected text is not echoed back."""
    return (
        f"{user}\n\nYour previous answer was rejected because {REJECTION_REASONS[problem]}. "
        'Answer again in two plain sentences using only the listed facts, as {"summary": "..."}.'
    )
