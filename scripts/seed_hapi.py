# ORIGIN: AI — script typed by Claude Code, reviewed by Kiel. The seed policy (HAPI as the state,
#   priority-first, the failure rule) is labeled H-spec below.
"""Load Synthea patient bundles into HAPI, priority patients first.

    python scripts/seed_hapi.py [--limit N] [--dry-run] [--verify]

Safe to re-run or kill at any point: before each bundle it asks HAPI whether that patient already
exists and skips it if so. Settings come from .env (see .env.example): FHIR_BASE_URL,
SEED_DATA_DIR, SEED_PRIORITY_FILE, SEED_LIMIT, SEED_TIMEOUT_SECONDS.
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from clinical_context.config import Settings
from synthea_data import (
    SYNTHEA_IDENTIFIER_SYSTEM,
    bundle_files,
    order_bundles,
    read_priority,
    uuid_from_filename,
)

FHIR_JSON = "application/fhir+json"
NO_CACHE = {"Cache-Control": "no-cache"}
PROBE_TIMEOUT_SECONDS = 60.0
FULL_SAMPLE_RESOURCES = 527_113  # from the recon; only used for the load-time estimate
VERIFIED_TYPES = ("Condition", "MedicationRequest", "AllergyIntolerance")


@dataclass(frozen=True)
class BundleResult:
    status: Literal["loaded", "skipped", "failed"]
    entries: int = 0
    seconds: float = 0.0
    detail: str = ""


def _search_total(
    client: httpx.Client, base_url: str, resource_type: str, params: dict[str, str]
) -> int:
    response = client.get(
        f"{base_url}/{resource_type}",
        params={**params, "_summary": "count"},
        headers=NO_CACHE,
    )
    response.raise_for_status()
    return response.json()["total"]


# ORIGIN: H-spec — Kiel's decision: HAPI itself is the state. There is no state file and no
#   count heuristic: before each bundle, ask HAPI whether that patient's Synthea identifier
#   already exists (a transaction is atomic, so a Patient means the bundle landed). The probe
#   sends Cache-Control: no-cache because HAPI reuses identical searches for 60 s by default, so
#   a stale "0 found" could otherwise cause a duplicate POST. Lines typed by Claude Code.
def probe_patient_exists(client: httpx.Client, base_url: str, uuid: str) -> bool:
    identifier = f"{SYNTHEA_IDENTIFIER_SYSTEM}|{uuid}"
    return _search_total(client, base_url, "Patient", {"identifier": identifier}) > 0


def _diagnostics(response: httpx.Response) -> str:
    try:
        issues = response.json().get("issue", [])
    except ValueError:
        return response.text[:300]
    texts = [i.get("diagnostics") or (i.get("details") or {}).get("text") for i in issues]
    return "; ".join(t for t in texts if t)[:600] or response.text[:300]


def _describe_failure(error: httpx.HTTPError) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}: {_diagnostics(error.response)}"
    return f"{type(error).__name__}: {error}"


def _exists_or_false(client: httpx.Client, base_url: str, uuid: str) -> bool:
    try:
        return probe_patient_exists(client, base_url, uuid)
    except httpx.HTTPError:
        return False


# ORIGIN: H-spec — Kiel's decision: on any error or timeout, ask HAPI once more (a timeout may
#   have committed). Found -> it counts as loaded. Not found -> record the file and HAPI's
#   diagnostics as failed, keep going, and let the run exit non-zero at the end. No $import.
#   Lines typed by Claude Code.
def load_bundle(
    client: httpx.Client, base_url: str, path: Path, timeout_seconds: float
) -> BundleResult:
    uuid = uuid_from_filename(path)
    if probe_patient_exists(client, base_url, uuid):
        return BundleResult("skipped", detail="already on the server")

    raw = path.read_bytes()
    entries = len(json.loads(raw).get("entry", []))
    started = time.monotonic()
    try:
        response = client.post(
            base_url, content=raw, headers={"Content-Type": FHIR_JSON}, timeout=timeout_seconds
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        seconds = time.monotonic() - started
        detail = _describe_failure(error)
        if _exists_or_false(client, base_url, uuid):
            return BundleResult("loaded", entries, seconds, f"committed despite: {detail}")
        return BundleResult("failed", entries, seconds, detail)
    return BundleResult("loaded", entries, time.monotonic() - started)


def _line(index: int, total: int, name: str, result: BundleResult) -> str:
    head = f"[{index:>4}/{total}] {name[:58]:58} {result.status:7}"
    if result.status == "skipped":
        return head
    rate = result.entries / result.seconds if result.seconds else 0
    return f"{head} entries={result.entries:>6}  {result.seconds:7.1f}s  {rate:7.0f} res/s"


def run_seed(
    client: httpx.Client, base_url: str, bundles: list[Path], timeout_seconds: float
) -> int:
    counts = {"loaded": 0, "skipped": 0, "failed": 0}
    resources = 0
    load_seconds = 0.0
    failures: list[tuple[str, str]] = []
    started = time.monotonic()

    for index, path in enumerate(bundles, start=1):
        result = load_bundle(client, base_url, path, timeout_seconds)
        counts[result.status] += 1
        print(_line(index, len(bundles), path.name, result), flush=True)
        if result.status == "loaded":
            resources += result.entries
            load_seconds += result.seconds
        elif result.status == "failed":
            failures.append((path.name, result.detail))
            print(f"           FAILED: {result.detail}", flush=True)

    print(
        f"\ndone in {time.monotonic() - started:.0f}s: "
        f"{counts['loaded']} loaded, {counts['skipped']} skipped, {counts['failed']} failed"
    )
    print(f"patients on HAPI now: {_search_total(client, base_url, 'Patient', {})}")
    if resources and load_seconds:
        rate = resources / load_seconds
        hours = FULL_SAMPLE_RESOURCES / rate / 3600
        print(
            f"measured {rate:.0f} resources/s over {resources} resources; "
            f"all {FULL_SAMPLE_RESOURCES:,} would take about {hours:.1f} h at this rate"
        )
    for name, detail in failures:
        print(f"FAILED {name}: {detail}", file=sys.stderr)
    return 1 if failures else 0


def _local_counts(path: Path) -> dict[str, int]:
    entries = json.loads(path.read_bytes()).get("entry", [])
    kinds = [e["resource"]["resourceType"] for e in entries]
    return {kind: kinds.count(kind) for kind in VERIFIED_TYPES}


def _hapi_patient_id(client: httpx.Client, base_url: str, uuid: str) -> str | None:
    response = client.get(
        f"{base_url}/Patient",
        params={"identifier": f"{SYNTHEA_IDENTIFIER_SYSTEM}|{uuid}", "_elements": "id"},
        headers=NO_CACHE,
    )
    response.raise_for_status()
    entries = response.json().get("entry", [])
    return entries[0]["resource"]["id"] if entries else None


def verify(client: httpx.Client, base_url: str, bundles: list[Path], priority: list[str]) -> bool:
    """For each priority patient, compare HAPI's per-type counts with the local file's counts."""
    by_uuid = {uuid_from_filename(p): p for p in bundles}
    all_match = True
    header = "".join(f"{kind:>22}" for kind in VERIFIED_TYPES)
    print(f"{'patient':24}{'HAPI id':>9}{header}")
    for uuid in priority:
        if uuid not in by_uuid:
            continue
        name = by_uuid[uuid].name.rsplit("_", 1)[0]
        hapi_id = _hapi_patient_id(client, base_url, uuid)
        if hapi_id is None:
            print(f"{name:24}{'-':>9}  not loaded")
            all_match = False
            continue
        local = _local_counts(by_uuid[uuid])
        cells = []
        for kind in VERIFIED_TYPES:
            on_hapi = _search_total(client, base_url, kind, {"patient": hapi_id})
            matches = on_hapi == local[kind]
            all_match &= matches
            cells.append(
                f"{f'{on_hapi:,} / {local[kind]:,}  {"ok" if matches else "MISMATCH"}':>22}"
            )
        print(f"{name:24}{hapi_id:>9}{''.join(cells)}")
    print(f"\npatients on HAPI: {_search_total(client, base_url, 'Patient', {})}")
    print(
        "cells read: HAPI count / count in the local file"
        + ("" if all_match else "  ** MISMATCH **")
    )
    return all_match


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, help="total bundles to load (overrides SEED_LIMIT)")
    parser.add_argument("--dry-run", action="store_true", help="print the load order and exit")
    parser.add_argument("--verify", action="store_true", help="compare HAPI counts to the files")
    args = parser.parse_args(argv)

    settings = Settings()
    files = bundle_files(settings.seed_data_dir)
    if not files:
        print(f"no bundles in {settings.seed_data_dir}; run scripts/download_synthea.py first")
        return 1
    priority = read_priority(settings.seed_priority_file)
    limit = args.limit if args.limit is not None else settings.seed_limit
    ordered, missing = order_bundles(files, priority, limit)
    for uuid in missing:
        print(f"warning: priority patient {uuid} has no file in {settings.seed_data_dir}")

    base_url = settings.fhir_base_url.rstrip("/")
    if args.dry_run:
        print(f"{len(ordered)} bundles to consider, in this order (P = priority):")
        for index, path in enumerate(ordered, start=1):
            flag = "P" if uuid_from_filename(path) in priority else " "
            print(f"  {index:>4} {flag} {path.name}")
        return 0

    with httpx.Client(timeout=PROBE_TIMEOUT_SECONDS) as client:
        try:
            if args.verify:
                return 0 if verify(client, base_url, files, priority) else 1
            return run_seed(client, base_url, ordered, settings.seed_timeout_seconds)
        except httpx.ConnectError:
            print(f"cannot reach HAPI at {base_url}; is `docker compose up` running?")
            return 2
        except KeyboardInterrupt:
            print("\ninterrupted: re-run to continue; loaded bundles will be skipped")
            return 130


if __name__ == "__main__":
    sys.exit(main())
