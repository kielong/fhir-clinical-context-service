# ORIGIN: AI — helper code typed by Claude Code, reviewed by Kiel.
"""Shared by the download, recon, fixture and seed scripts: the pinned sample and its filenames."""

import re
from pathlib import Path

# ORIGIN: H-spec — Kiel's decision: use only this pinned 1K FHIR R4 sample (not "latest", which
#   is 100 patients), and refuse anything whose size, checksum, or patient-file count differs.
#   Lines typed by Claude Code.
ZIP_URL = (
    "https://synthetichealth.github.io/synthea-sample-data/downloads/"
    "synthea_sample_data_fhir_r4_sep2019.zip"
)
ZIP_NAME = "synthea_sample_data_fhir_r4_sep2019.zip"
ZIP_BYTES = 85_042_887
ZIP_SHA256 = "a6fc595d9c0f4c646746af42f861b5a12d03c856af158dd837c764dfb81b66f8"
EXPECTED_PATIENT_FILES = 1180
DOWNLOADS_PAGE = "https://synthea.mitre.org/downloads"

# Every patient in the sample carries its filename UUID under this identifier system.
SYNTHEA_IDENTIFIER_SYSTEM = "https://github.com/synthetichealth/synthea"

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_UUID_LINE = re.compile(rf"^{_UUID}$")
_FILENAME_UUID = re.compile(rf"_({_UUID})\.json$")


def uuid_from_filename(path: str | Path) -> str:
    """`Aaron697_Brekke496_2fa15bc7-....json` -> `2fa15bc7-...` (the Synthea identifier)."""
    name = Path(path).name
    match = _FILENAME_UUID.search(name)
    if not match:
        raise ValueError(f"no Synthea UUID at the end of the filename: {name}")
    return match.group(1).lower()


def read_priority(path: str | Path) -> list[str]:
    """UUIDs to load first, in file order. Blank lines and `#` comments are ignored."""
    priority: list[str] = []
    for number, raw in enumerate(Path(path).read_text().splitlines(), start=1):
        text = raw.split("#", 1)[0].strip()
        if not text:
            continue
        if not _UUID_LINE.match(text):
            raise ValueError(f"{path}: line {number} is not a UUID: {text!r}")
        if text.lower() not in priority:
            priority.append(text.lower())
    return priority


def bundle_files(data_dir: str | Path) -> list[Path]:
    """Every patient bundle in the extracted sample, alphabetical."""
    return sorted(Path(data_dir).glob("*.json"), key=lambda p: p.name)


# ORIGIN: H-spec — Kiel's decision: priority patients load first (so the demo and evaluation
#   patients exist even if the full load is unfinished), then everything else alphabetically;
#   the limit counts priority bundles first; a priority UUID with no file is reported, never
#   silently dropped. Lines typed by Claude Code.
def order_bundles(
    files: list[Path], priority: list[str], limit: int | None
) -> tuple[list[Path], list[str]]:
    """Return (bundles in load order, priority UUIDs that have no file)."""
    if limit is not None and limit < 1:
        raise ValueError(f"limit must be at least 1 (got {limit}); leave it unset to load all")
    by_uuid = {uuid_from_filename(f): Path(f) for f in files}
    first = [by_uuid[u] for u in priority if u in by_uuid]
    missing = [u for u in priority if u not in by_uuid]
    chosen = set(first)
    rest = sorted((Path(f) for f in files if Path(f) not in chosen), key=lambda p: p.name)
    ordered = first + rest
    return (ordered if limit is None else ordered[:limit]), missing
