# ORIGIN: AI — test cases drafted and typed by Claude Code, reviewed by Kiel
"""The download script's guards. Tiny zips built in tmp_path; nothing is downloaded."""

import hashlib
import zipfile
from pathlib import Path

import pytest

from download_synthea import extract_patient_bundles, sha256_of


def _make_zip(path: Path, json_names: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name in json_names:
            archive.writestr(f"fhir/{name}", "{}")
    return path


def test_sha256_matches_hashlib(tmp_path):
    target = tmp_path / "data.bin"
    target.write_bytes(b"synthea")

    assert sha256_of(target) == hashlib.sha256(b"synthea").hexdigest()


def test_extraction_writes_the_bundles_under_fhir(tmp_path):
    archive = _make_zip(tmp_path / "s.zip", ["a_1.json", "b_2.json"])

    files = extract_patient_bundles(archive, tmp_path / "out", expected_count=2)

    assert sorted(p.name for p in files) == ["a_1.json", "b_2.json"]
    assert all(p.parent == tmp_path / "out" / "fhir" for p in files)


def test_a_zip_with_the_wrong_number_of_patient_files_is_refused_before_extracting(tmp_path):
    # e.g. the 100-patient "latest" sample, or a different Synthea export
    archive = _make_zip(tmp_path / "s.zip", ["a_1.json", "b_2.json"])

    with pytest.raises(ValueError, match="1180"):
        extract_patient_bundles(archive, tmp_path / "out", expected_count=1180)

    assert not (tmp_path / "out").exists()
