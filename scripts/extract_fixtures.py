# ORIGIN: AI — script typed by Claude Code, reviewed by Kiel. Which patients become fixtures is
#   Kiel's decision (scripts/seed_priority.txt).
"""Write small, real test fixtures for the priority patients from the local Synthea files.

    python scripts/extract_fixtures.py

Each fixture holds the raw Patient plus that patient's Condition, MedicationRequest and
AllergyIntolerance resources, unmodified. Long medication histories are trimmed to keep the files
small: every active/on-hold medication is kept, plus the most recent stopped ones. The full-bundle
totals are recorded in `_meta` so tests know what was cut.

The data is from the Synthea 1K sample (https://github.com/synthetichealth/synthea, Apache-2.0).
"""

import json
import sys
from pathlib import Path

from clinical_context.config import Settings
from synthea_data import bundle_files, order_bundles, read_priority, uuid_from_filename

FIXTURE_DIR = Path("tests/fixtures/real")
KEEP_STOPPED_MEDICATIONS = 30
CURRENT_MEDICATION = {"active", "on-hold"}


def build_fixture(path: Path, keep_stopped: int = KEEP_STOPPED_MEDICATIONS) -> dict:
    resources = [e["resource"] for e in json.loads(path.read_bytes())["entry"]]

    def of(kind: str) -> list[dict]:
        return [r for r in resources if r["resourceType"] == kind]

    medications = of("MedicationRequest")
    stopped = [m for m in medications if m["status"] not in CURRENT_MEDICATION]
    newest_stopped = sorted(stopped, key=lambda m: m["authoredOn"], reverse=True)[:keep_stopped]
    kept_ids = {id(m) for m in medications if m["status"] in CURRENT_MEDICATION}
    kept_ids |= {id(m) for m in newest_stopped}
    kept = [m for m in medications if id(m) in kept_ids]  # original order preserved

    (patient,) = of("Patient")
    conditions, allergies = of("Condition"), of("AllergyIntolerance")
    return {
        "_meta": {
            "origin": "Synthea 1K FHIR R4 sample (Apache-2.0), unmodified resources",
            "source_file": path.name,
            "synthea_uuid": uuid_from_filename(path),
            "conditions_in_bundle": len(conditions),
            "medications_in_bundle": len(medications),
            "medications_kept": len(kept),
            "allergies_in_bundle": len(allergies),
        },
        "patient": patient,
        "conditions": conditions,
        "medications": kept,
        "allergies": allergies,
    }


def fixture_name(path: Path) -> str:
    """`Aaron697_Brekke496_<uuid>.json` -> `Aaron697_Brekke496`."""
    return path.name.removesuffix(f"_{uuid_from_filename(path)}.json")


def main() -> int:
    settings = Settings()
    files = bundle_files(settings.seed_data_dir)
    if not files:
        print(f"no bundles in {settings.seed_data_dir}; run scripts/download_synthea.py first")
        return 1
    priority = read_priority(settings.seed_priority_file)
    chosen, missing = order_bundles(files, priority, limit=None)
    if missing:
        print(f"priority patients with no file: {missing}")
        return 1

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for path in chosen[: len(priority)]:
        fixture = build_fixture(path)
        target = FIXTURE_DIR / f"{fixture_name(path)}.json"
        target.write_text(json.dumps(fixture, indent=2) + "\n")
        meta = fixture["_meta"]
        print(
            f"{target.name:34} conditions {meta['conditions_in_bundle']:>3}  "
            f"medications {meta['medications_kept']:>3}/{meta['medications_in_bundle']:<5} "
            f"allergies {meta['allergies_in_bundle']:>2}  {target.stat().st_size / 1024:6.0f} KB"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
