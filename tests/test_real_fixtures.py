# ORIGIN: AI — test cases and expected values typed by Claude Code (from a recon of the real
#   Synthea sample), reviewed by Kiel.
"""The real-data fixtures are faithful excerpts of the Synthea sample, pinned to known counts.

These counts were measured on the full bundles. If a fixture drifts (regenerated from a different
sample, trimmed wrongly), assembly tests built on it would silently test the wrong thing.
"""

import json
from pathlib import Path

import pytest

from synthea_data import SYNTHEA_IDENTIFIER_SYSTEM

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "real"

# uuid: (conditions total, conditions active, meds total in bundle, meds active,
#        allergies active, allergies inactive)
GOLDEN = {
    "2fa15bc7-8866-461a-9000-f739e425860a": (10, 5, 1, 0, 0, 0),  # Aaron697
    "0979f4fe-08c5-414e-ba1c-6ccf852bcce4": (23, 20, 1275, 10, 0, 0),  # Floyd420
    "9da0dcfc-05e3-4e8e-95ff-b04b56f748be": (28, 24, 9, 7, 0, 0),  # Shelly431
    "5919de03-6363-41a7-b251-f5be75149adc": (16, 10, 19, 14, 2, 0),  # Jose871
    "e2129449-9c68-4155-a826-e22091aa4742": (0, 0, 0, 0, 0, 0),  # Alicia629
    "f7f63ca8-d282-4520-9a68-3177e2a5db6f": (5, 2, 4, 2, 5, 1),  # Andreas188
    "9dc305b0-c821-49f3-817c-58e853bce8b1": (7, 1, 4, 3, 6, 4),  # Beatriz277
    "9e2653fc-49e0-4b2e-86f8-e664bbe07be3": (10, 6, 7, 3, 7, 0),  # Adam631
    "20123c13-40e7-4134-8a18-58c57be98c74": (1, 0, 0, 0, 0, 0),  # Alan320
    "2c57a897-8381-44a6-920f-e074fa6f74cf": (20, 14, 192, 6, 0, 0),  # Lorenzo669
}


def _load_all() -> dict[str, dict]:
    fixtures = {}
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        fixtures[data["_meta"]["synthea_uuid"]] = data
    return fixtures


def _status(resource: dict) -> str:
    return resource["clinicalStatus"]["coding"][0]["code"]


def test_there_is_a_fixture_for_every_priority_patient():
    assert set(_load_all()) == set(GOLDEN)


@pytest.mark.parametrize("uuid", sorted(GOLDEN))
def test_fixture_patient_is_the_named_synthea_patient(uuid):
    patient = _load_all()[uuid]["patient"]

    assert patient["resourceType"] == "Patient"
    identifiers = {(i["system"], i["value"]) for i in patient["identifier"]}
    assert (SYNTHEA_IDENTIFIER_SYSTEM, uuid) in identifiers


@pytest.mark.parametrize("uuid", sorted(GOLDEN))
def test_fixture_condition_and_allergy_counts_match_the_full_bundle(uuid):
    conds_total, conds_active, _, _, allergies_active, allergies_inactive = GOLDEN[uuid]
    fixture = _load_all()[uuid]

    conditions = fixture["conditions"]
    allergies = fixture["allergies"]
    assert len(conditions) == conds_total
    assert sum(_status(c) == "active" for c in conditions) == conds_active
    assert sum(_status(a) == "active" for a in allergies) == allergies_active
    assert sum(_status(a) == "inactive" for a in allergies) == allergies_inactive


@pytest.mark.parametrize("uuid", sorted(GOLDEN))
def test_fixture_keeps_every_active_medication_and_records_the_full_bundle_total(uuid):
    _, _, meds_total, meds_active, _, _ = GOLDEN[uuid]
    fixture = _load_all()[uuid]

    kept = fixture["medications"]
    assert fixture["_meta"]["medications_in_bundle"] == meds_total
    assert sum(m["status"] == "active" for m in kept) == meds_active  # none of the active ones cut
    assert len(kept) <= meds_active + 30  # stopped medications are trimmed to a small sample
    assert len(kept) == min(meds_total, meds_active + 30)
