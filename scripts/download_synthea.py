# ORIGIN: AI — script typed by Claude Code, reviewed by Kiel. The pinned-sample rules it enforces
#   are Kiel's decisions (see synthea_data.py).
"""Download and extract the pinned Synthea 1K FHIR R4 sample into data/synthea/.

    python scripts/download_synthea.py

Safe to re-run: it skips the download if the verified zip is already there and skips extraction
if all 1,180 patient bundles are already extracted.
"""

import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

import httpx

from synthea_data import (
    DOWNLOADS_PAGE,
    EXPECTED_PATIENT_FILES,
    ZIP_BYTES,
    ZIP_NAME,
    ZIP_SHA256,
    ZIP_URL,
)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def zip_is_verified(path: Path) -> bool:
    return path.is_file() and path.stat().st_size == ZIP_BYTES and sha256_of(path) == ZIP_SHA256


def download(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".part")
    print(f"downloading {ZIP_URL}")
    with httpx.stream("GET", ZIP_URL, follow_redirects=True, timeout=60) as response:
        response.raise_for_status()
        done, next_report = 0, 10
        with partial.open("wb") as handle:
            for chunk in response.iter_bytes(1024 * 1024):
                handle.write(chunk)
                done += len(chunk)
                percent = 100 * done // ZIP_BYTES
                if percent >= next_report:
                    print(f"  {percent}%")
                    next_report += 10
    partial.replace(target)


def extract_patient_bundles(zip_path: Path, dest_dir: Path, expected_count: int) -> list[Path]:
    """Extract `fhir/*.json` to dest_dir, refusing first if the count is not exactly as expected."""
    with zipfile.ZipFile(zip_path) as archive:
        members = [n for n in archive.namelist() if n.startswith("fhir/") and n.endswith(".json")]
        if len(members) != expected_count:
            raise ValueError(
                f"expected exactly {expected_count} patient bundles in {zip_path.name}, "
                f"found {len(members)} (wrong sample?)"
            )
        for member in members:
            archive.extract(member, dest_dir)
    return sorted((dest_dir / m for m in members), key=lambda p: p.name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dest", default="data/synthea", type=Path)
    dest: Path = parser.parse_args(argv).dest

    fhir_dir = dest / "fhir"
    if fhir_dir.is_dir() and len(list(fhir_dir.glob("*.json"))) == EXPECTED_PATIENT_FILES:
        print(f"already extracted: {EXPECTED_PATIENT_FILES} bundles in {fhir_dir}")
        return 0

    zip_path = dest / ZIP_NAME
    if zip_is_verified(zip_path):
        print(f"using verified zip {zip_path}")
    else:
        try:
            download(zip_path)
        except httpx.HTTPError as error:
            print(f"download failed: {error}", file=sys.stderr)
            print(
                f"Download {ZIP_NAME} manually from {DOWNLOADS_PAGE} into {dest}/ and re-run.\n"
                f"It must be {ZIP_BYTES} bytes with sha256 {ZIP_SHA256}.",
                file=sys.stderr,
            )
            return 1
        if not zip_is_verified(zip_path):
            zip_path.unlink()
            print(
                f"downloaded file failed the size/sha256 check (expected {ZIP_BYTES} bytes, "
                f"sha256 {ZIP_SHA256}); it was deleted.",
                file=sys.stderr,
            )
            return 1
        print("size and sha256 verified")

    files = extract_patient_bundles(zip_path, dest, EXPECTED_PATIENT_FILES)
    print(f"extracted {len(files)} patient bundles to {fhir_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
