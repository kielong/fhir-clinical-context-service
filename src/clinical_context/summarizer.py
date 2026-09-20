# ORIGIN: H-spec — the policy below is Kiel's decisions: the model writes prose only and never sees
#   a source, an id, or the patient's name; the exact things it is forbidden to say (invented
#   facts, determinations, control or prognosis claims, "currently has" for a deceased patient);
#   the checks every answer must pass; one retry that says what was wrong (a temperature-0 retry
#   of the same prompt would just repeat itself); no retry on a timeout; any failure leaves the
#   facts untouched and reports why. Lines typed by Claude Code. Edge cases nobody specified are
#   labeled AI where they occur.
"""The only place a model is used: it writes a two-sentence summary of an assembled packet.

The model is a writer at the end of a deterministic pipeline. It is given plain lines derived
from the packet, and it may return exactly one thing: {"summary": "<two sentences>"}. Everything
it writes is checked; anything that fails a check is thrown away and the packet is returned with
the summary marked unavailable. The facts, the sources and the gaps never depend on the model.

SAME INPUT, SAME OUTPUT
  The prompt is a pure function of the packet: no ids, no clock, nothing that varies between
  runs. The request pins the decoding (temperature 0, top_k 1, a fixed seed, fixed context and
  output sizes) and constrains the output to a one-field JSON schema. Together that makes the same
  packet produce the same text on the same model and hardware. Anything the model still gets
  wrong is caught by validate_summary, so what a reviewer sees is either a checked summary or none.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ConfigDict

from .config import Settings
from .models import ClinicalContextPacket, SummaryBlock, SummaryReason

logger = logging.getLogger("clinical_context.summarizer")

MAX_SUMMARY_CHARS = 400
MAX_ATTEMPTS = 2  # one try, and one retry that says what was wrong

# ------------------------------------------------------------------------------ the prompt

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
7. If the patient is deceased, say so, and never describe them as currently on treatment.
8. If a list says none, you may say none is recorded. Never say more than the lists say.

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


# ORIGIN: H-spec — Kiel's decision: gaps go to the model in plain words (no resource names, no
#   ids), with how many records were excluded as no longer active. Lines typed by Claude Code.
#   The exact wording is Claude Code's.
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
    conditions = [strip_semantic_tag(c.display) for c in packet.conditions]
    medications = [
        strip_semantic_tag(m.display) + (" (on hold)" if m.status == "on-hold" else "")
        for m in packet.medications
    ]
    allergies = [
        strip_semantic_tag(a.display)
        + (f" (criticality: {a.criticality})" if a.criticality else "")
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
    return SYSTEM_PROMPT, "\n".join(lines)


# ------------------------------------------------------------------------------ what it may say

_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
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


def validate_summary(text: str) -> str | None:
    """None if the summary may be shown, else a short code for the first rule it breaks."""
    text = text.strip()
    if not text:
        return "empty"
    if len(text) > MAX_SUMMARY_CHARS:
        return "too_long"
    if len(_SENTENCE_BREAK.split(text)) > 2:
        return "too_many_sentences"
    if _IDENTIFIER.search(text):
        return "contains_identifier"
    if _DETERMINATION.search(text):
        return "determination_language"
    if _CONTROL.search(text):
        return "control_claim"
    return None


# ORIGIN: AI — added by Claude Code after a live run: a 3B model told a reviewer "no reported
#   allergies" for a patient whose record lists five. validate_summary looks only at the words, so
#   it could not catch that. This is a deliberately narrow, deterministic check for the clearest
#   possible falsehood: saying none of something is recorded when the packet lists some. It is not
#   a general fact-checker; other unfaithful wording is what the evaluation measures.
_NONE_OF = (
    r"\b(?:no|without)\s+(?:any\s+)?"
    r"(?:(?:recorded|reported|known|documented|active|current|on-hold|or|and)\s+)*"
)
_CLAIMS_NONE = {
    "allergies": re.compile(_NONE_OF + r"allerg", re.IGNORECASE),
    "medications": re.compile(_NONE_OF + r"(?:medications?|prescriptions?|drugs?)", re.IGNORECASE),
    "conditions": re.compile(_NONE_OF + r"(?:conditions?|diagnos[ei]s|problems?)", re.IGNORECASE),
}


def contradicts_facts(text: str, packet: ClinicalContextPacket) -> str | None:
    """ "contradicts_facts" if the text says none are recorded for a list the packet fills."""
    listed = {
        "allergies": packet.allergies,
        "medications": packet.medications,
        "conditions": packet.conditions,
    }
    for section, pattern in _CLAIMS_NONE.items():
        if listed[section] and pattern.search(text):
            return "contradicts_facts"
    return None


_WHY_REJECTED = {
    "invalid_json": "it was not valid JSON with a single string field named summary",
    "empty": "it was empty",
    "too_long": "it was too long",
    "too_many_sentences": "it had more than two sentences",
    "contains_identifier": "it contained an identifier",
    "determination_language": (
        "it used determination language (approve, deny, authorize, medically necessary, eligible)"
    ),
    "control_claim": "it claimed a condition was controlled, stable, improving, or worsening",
    "contradicts_facts": "it said none were recorded for something the record lists, which "
    "contradicted the listed facts",
}


def _with_correction(user: str, problem: str) -> str:
    """The retry prompt: same facts, plus what was wrong (a repeat of the same prompt at
    temperature 0 would just produce the same answer). The rejected text is not echoed back."""
    return (
        f"{user}\n\nYour previous answer was rejected because {_WHY_REJECTED[problem]}. "
        'Answer again in two plain sentences using only the listed facts, as {"summary": "..."}.'
    )


# ------------------------------------------------------------------------------ asking Ollama


class _SummaryOnly(BaseModel):
    """The one thing the model may return."""

    model_config = ConfigDict(extra="forbid")
    summary: str


RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}

# ORIGIN: H-spec — Kiel's decision: decoding is pinned so the same prompt gives the same text.
#   temperature 0 and a fixed seed are the decision; top_k 1 (always take the single most likely
#   token, so nothing is sampled at all) is Claude Code's addition to close the remaining gap.
#   Lines typed by Claude Code.
SEED = 0


def _options(settings: Settings) -> dict:
    return {
        "temperature": 0,
        "top_k": 1,
        "seed": SEED,
        "num_ctx": settings.ollama_num_ctx,
        "num_predict": settings.ollama_num_predict,
    }


class _ModelUnreachable(Exception):
    """Ollama did not answer, or answered with an error (including 'model not found')."""


class _InvalidOutput(Exception):
    """Ollama answered, but not with exactly {"summary": <string>}."""


# ORIGIN: AI — edge cases added by Claude Code: any error status from Ollama (a 404 means the model
#   is not pulled) counts as "unreachable", and a truncated or wrong-shaped body is "invalid".
async def _ask(http: httpx.AsyncClient, settings: Settings, system: str, user: str):
    body = {
        "model": settings.ollama_model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        "format": RESPONSE_SCHEMA,
        "options": _options(settings),
        "keep_alive": settings.ollama_keep_alive,
    }
    try:
        response = await http.post(
            f"{settings.ollama_host}/api/chat", json=body, timeout=settings.ollama_timeout_seconds
        )
    except httpx.TimeoutException:
        raise  # a slow model is reported as a timeout, not as unreachable
    except httpx.HTTPError as error:
        raise _ModelUnreachable(type(error).__name__) from error
    if response.status_code != 200:
        raise _ModelUnreachable(f"HTTP {response.status_code}")
    try:
        data = response.json()
        summary = _SummaryOnly.model_validate_json(data["message"]["content"]).summary
    except (ValueError, KeyError, TypeError) as error:
        raise _InvalidOutput from error
    return summary, _timings(data)


_TIMING_FIELDS = {  # our name: (Ollama's field, divisor from nanoseconds)
    "total_ms": ("total_duration", 1_000_000),
    "load_ms": ("load_duration", 1_000_000),
    "prompt_tokens": ("prompt_eval_count", 1),
    "prompt_ms": ("prompt_eval_duration", 1_000_000),
    "generated_tokens": ("eval_count", 1),
    "generation_ms": ("eval_duration", 1_000_000),
}


def _timings(data: dict) -> dict[str, int] | None:
    found = {
        name: data[field] // divisor
        for name, (field, divisor) in _TIMING_FIELDS.items()
        if isinstance(data.get(field), int)
    }
    return found or None


# ------------------------------------------------------------------------------ the result


@dataclass(frozen=True)
class SummaryResult:
    block: SummaryBlock
    timings: dict[str, int] | None  # Ollama's own breakdown of the last answer it gave
    elapsed_ms: int | None  # wall time of the whole call, retry included; None if never called
    attempts: int
    cached: bool = False  # True when these exact words were remembered from an earlier call


def _unavailable(settings: Settings, reason: SummaryReason) -> SummaryBlock:
    return SummaryBlock(text=None, status="unavailable", model=settings.ollama_model, reason=reason)


# ORIGIN: H-spec — Kiel's decision: fail closed. Retry once, and only for a bad answer (invalid
#   JSON or a policy violation). A timeout or an unreachable model is not retried, and one time
#   budget covers the whole call. Whatever goes wrong, the result is "unavailable" plus a reason;
#   it never raises and never returns unchecked text. Lines typed by Claude Code.
async def _generate(
    packet: ClinicalContextPacket,
    system: str,
    user: str,
    *,
    http: httpx.AsyncClient,
    settings: Settings,
) -> SummaryResult:
    started = time.perf_counter()
    attempts = 0
    problem: str | None = None  # why the previous attempt was rejected
    reason: SummaryReason = "invalid_output"
    timings = None
    block: SummaryBlock | None = None

    try:
        async with asyncio.timeout(settings.ollama_timeout_seconds):
            while attempts < MAX_ATTEMPTS and block is None:
                attempts += 1
                prompt = user if problem is None else _with_correction(user, problem)
                try:
                    text, timings = await _ask(http, settings, system, prompt)
                except _InvalidOutput:
                    problem, reason = "invalid_json", "invalid_output"
                    continue
                problem = validate_summary(text) or contradicts_facts(text, packet)
                if problem is None:
                    block = SummaryBlock(
                        text=text.strip(),
                        status="generated",
                        model=settings.ollama_model,
                        reason=None,
                    )
                else:
                    reason = "policy_violation"
    except (httpx.TimeoutException, TimeoutError):
        block = _unavailable(settings, "timeout")
    except _ModelUnreachable as error:
        logger.warning("model unreachable: %s", error)
        block = _unavailable(settings, "model_unreachable")

    if block is None:
        block = _unavailable(settings, reason)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "summary outcome=%s cache=miss model=%s attempts=%d elapsed_ms=%d timings=%s",
        block.reason or "generated",
        settings.ollama_model,
        attempts,
        elapsed_ms,
        json.dumps(timings),
    )
    return SummaryResult(block=block, timings=timings, elapsed_ms=elapsed_ms, attempts=attempts)


# ------------------------------------------------------------------------------ same words


# ORIGIN: H-spec — Kiel's decision: the same packet must give the same words. Ollama's prompt
#   cache changes the arithmetic, so even at temperature 0 with a fixed seed the same prompt can
#   come back worded differently depending on what ran before it (measured: three different
#   answers for one patient across cold, warm and partly warm states). Decoding cannot be pinned
#   past that, so the finished summary is pinned instead: remembered under a hash of exactly what
#   produced it (model, decoding options, prompt) and returned verbatim next time. Only finished,
#   checked summaries are remembered, never failures. The gate lets one model call run at a time,
#   which also means simultaneous identical requests cause one generation. Lines typed by Claude
#   Code. The cache lives in this process's memory only (it holds summary text, which is patient
#   data): it is empty after a restart, so a restart can re-word a summary once.
class SummaryCache:
    def __init__(self, maxsize: int) -> None:
        self._maxsize = maxsize
        self._entries: OrderedDict[str, SummaryBlock] = OrderedDict()
        self.gate = asyncio.Lock()  # one model call at a time

    def get(self, key: str) -> SummaryBlock | None:
        block = self._entries.get(key)
        if block is not None:
            self._entries.move_to_end(key)
        return block

    def put(self, key: str, block: SummaryBlock) -> None:
        if self._maxsize <= 0:
            return
        self._entries[key] = block
        self._entries.move_to_end(key)
        while len(self._entries) > self._maxsize:
            self._entries.popitem(last=False)  # forget the least recently used


def _cache_key(settings: Settings, system: str, user: str) -> str:
    """Everything that decides what the model writes: change any of it and the key changes."""
    identity = {
        "model": settings.ollama_model,
        "options": _options(settings),
        "system": system,
        "user": user,
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _served_from_memory(settings: Settings, block: SummaryBlock) -> SummaryResult:
    logger.info("summary outcome=generated cache=hit model=%s", settings.ollama_model)
    return SummaryResult(block=block, timings=None, elapsed_ms=0, attempts=0, cached=True)


async def summarize(
    packet: ClinicalContextPacket,
    *,
    http: httpx.AsyncClient,
    settings: Settings,
    cache: SummaryCache | None = None,
) -> SummaryResult:
    system, user = build_prompt(packet)
    if cache is None:
        return await _generate(packet, system, user, http=http, settings=settings)

    key = _cache_key(settings, system, user)
    remembered = cache.get(key)
    if remembered is not None:
        return _served_from_memory(settings, remembered)
    async with cache.gate:
        remembered = cache.get(key)  # a request ahead of us in line may have just written it
        if remembered is not None:
            return _served_from_memory(settings, remembered)
        result = await _generate(packet, system, user, http=http, settings=settings)
        if result.block.status == "generated":
            cache.put(key, result.block)
        return result


# ------------------------------------------------------------------------------ warm-up


# ORIGIN: AI — added by Claude Code: Ollama unloads an idle model after a few minutes, and loading
#   a 3B model is what stalls the first request. An empty chat request loads it ahead of time.
#   It must never stop the service from starting, so any failure is only logged.
async def warm_up(http: httpx.AsyncClient, settings: Settings) -> None:
    try:
        await http.post(
            f"{settings.ollama_host}/api/chat",
            json={
                "model": settings.ollama_model,
                "messages": [],
                "keep_alive": settings.ollama_keep_alive,
            },
            timeout=settings.ollama_timeout_seconds,
        )
        logger.info("model warm-up finished model=%s", settings.ollama_model)
    except Exception as error:  # deliberately broad: warm-up is best effort
        logger.warning("model warm-up failed: %s", type(error).__name__)
