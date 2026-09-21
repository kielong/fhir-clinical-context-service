# ORIGIN: AI — packet builders typed by Claude Code, reviewed by Kiel.
"""Assembled packets for tests that need a packet but are not about assembly."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import synthetic as syn
from clinical_context.packet.assembly import assemble_packet
from clinical_context.packet.models import ClinicalContextPacket

FIXTURES = Path(__file__).parent / "fixtures" / "real"
AS_OF = date(2019, 9, 16)
GENERATED_AT = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def packet_from(
    *,
    patient: dict,
    conditions=(),
    medications=(),
    allergies=(),
    patient_id_echo: str = "demo-id",
    generated_at: datetime = GENERATED_AT,
    list_cap: int = 25,
) -> ClinicalContextPacket:
    return assemble_packet(
        patient=patient,
        conditions=list(conditions),
        medications=list(medications),
        allergies=list(allergies),
        as_of=AS_OF,
        as_of_source="config",
        generated_at=generated_at,
        list_cap=list_cap,
        patient_id_echo=patient_id_echo,
    )


def real_packet(name: str, **overrides) -> ClinicalContextPacket:
    fixture = json.loads((FIXTURES / f"{name}.json").read_text())
    return packet_from(
        patient=fixture["patient"],
        conditions=fixture["conditions"],
        medications=fixture["medications"],
        allergies=fixture["allergies"],
        **overrides,
    )


def busy_synthetic_packet(count: int = 25) -> ClinicalContextPacket:
    """The largest packet the service can produce: every list at the cap."""
    return packet_from(
        patient=syn.patient("1000"),
        conditions=[
            syn.condition(f"c{i}", display=f"Chronic condition number {i} (disorder)")
            for i in range(count)
        ],
        medications=[
            syn.medication(f"m{i}", drug=f"Some Medicine {i} 500 MG Oral Tablet")
            for i in range(count)
        ],
        allergies=[syn.allergy(f"a{i}", display=f"Allergy to substance {i}") for i in range(count)],
    )
