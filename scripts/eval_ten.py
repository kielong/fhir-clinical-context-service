#!/usr/bin/env python3
# ORIGIN: AI — script typed by Claude Code from the agreed evaluation flow, reviewed by Kiel. What
#   it checks (a cited source resolves, is this patient's, and has the status the packet claims) and
#   that it never scores fairness are Kiel's decisions. Claude Code's additions, not yet adopted:
#   the check that a fact's text is the record's own, the completeness check against HAPI's counts,
#   waiting for a timed-out summary, the independent re-check of the returned text, the wording
#   flags, saving results as it goes, and the exit code.
"""Check the service's packets against the record they cite.

For each patient it asks the running API for the packet, then reads the record straight from HAPI
and checks, independently of the service:

  * every `source` the packet cites exists, belongs to this patient, carries the status the packet
    claims, and has the name the packet shows for it;
  * nothing is missing: for each list, what the packet shows plus what it says it left out equals
    the number of records HAPI holds for the patient.

It does not judge whether a summary is fair: that is a person's call, so the summary text is hidden
unless you ask for it. Wording that deserves a closer read (present-tense treatment, a deceased
patient never called deceased) is flagged, never scored.

  python scripts/eval_ten.py --file eval/ten_patients.txt              # the ten, one table each
  python scripts/eval_ten.py --file eval/ten_patients.txt --show-summary
  python scripts/eval_ten.py --batch 100 --seed 1 --out eval/batch.jsonl
  python scripts/eval_ten.py --batch 100 --seed 1 --list               # who would be sampled

Exit status: 0 if everything checked out (and, for a batch, the schema-valid bar was met);
1 if anything failed or a patient could not be evaluated; 2 if nothing could be reached.
"""

import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from clinical_context.config import Settings
from clinical_context.llm.checks import validate_summary
from eval_helpers import (
    SECTIONS,
    BatchSummary,
    PatientResult,
    SourceCheck,
    account_for,
    check_claim,
    claims_of,
    sample_uuids,
    summarize_batch,
    wilson_interval,
    wording_flags,
)
from synthea_data import bundle_files, read_priority

# The acceptance bar (see eval/ten_patients.md). Sources are 100% or the run fails.
SCHEMA_VALID_BAR = 0.95
RETRY_PAUSE_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 150
FHIR_TYPES = {
    "conditions": "Condition",
    "medications": "MedicationRequest",
    "allergies": "AllergyIntolerance",
}
_FHIR_HEADERS = {"Accept": "application/fhir+json", "Cache-Control": "no-cache"}


def fetch_resource(client: httpx.Client, fhir_base: str, source: str) -> dict | None:
    """The record a source points at, or None if HAPI says it does not exist."""
    response = client.get(f"{fhir_base}/{source}", headers=_FHIR_HEADERS)
    if response.status_code in (404, 410):
        return None
    response.raise_for_status()  # a failing HAPI must not look like "unresolved" sources
    return response.json()


def fetch_total(client: httpx.Client, fhir_base: str, type_name: str, fhir_id: str) -> int:
    """How many records of one type HAPI holds for a patient, by its own count. Asked for fresh:
    HAPI reuses identical search results for a minute."""
    response = client.get(
        f"{fhir_base}/{type_name}",
        params={"patient": fhir_id, "_summary": "count"},
        headers=_FHIR_HEADERS,
    )
    response.raise_for_status()
    return response.json()["total"]


def _describe_failure(error: httpx.HTTPError) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"HAPI answered HTTP {error.response.status_code}"
    return f"{type(error).__name__} talking to HAPI"


def evaluate(
    client: httpx.Client,
    api: str,
    fhir: str,
    patient_id: str,
    *,
    patience_s: float,
    pause: Callable[[float], None] = time.sleep,
) -> tuple[PatientResult, dict | None]:
    """Fetch one patient's packet and check it against HAPI.

    A summary that timed out is asked for again every few seconds until `patience_s` has passed:
    the service keeps generating after it stops waiting and remembers the answer.
    """
    started = time.perf_counter()
    packet = None
    status = 0
    retries = 0
    first_llm_ms = None
    while True:
        try:
            response = client.get(f"{api}/v1/patients/{patient_id}/clinical-context")
        except httpx.HTTPError:
            status, packet = 0, None
            break
        status = response.status_code
        packet = response.json() if status == 200 else None
        if packet is None:
            break
        if retries == 0:
            first_llm_ms = (packet["meta"]["timings_ms"] or {}).get("llm")
        timed_out = packet["summary"]["reason"] == "timeout"
        if not timed_out or time.perf_counter() - started >= patience_s:
            break
        pause(RETRY_PAUSE_SECONDS)
        retries += 1
    wall_ms = int((time.perf_counter() - started) * 1000)

    if packet is None:
        return PatientResult(patient_id, status, None, None, (0, 0, 0), (0, 0, 0), wall_ms), None

    fhir_id = packet["patient"]["fhir_id"]
    checks: list[SourceCheck] = []
    accounts = []
    problem = None
    try:
        for claim in claims_of(packet):
            checks.append(check_claim(claim, fetch_resource(client, fhir, claim.source), fhir_id))
        totals = {s: fetch_total(client, fhir, FHIR_TYPES[s], fhir_id) for s in SECTIONS}
        accounts = account_for(packet, totals)
    except httpx.HTTPError as error:
        problem = _describe_failure(error)  # keep what was learned; do not lose the whole run

    summary = packet["summary"]
    text = summary["text"]
    violation = validate_summary(text) if text else None
    flags = wording_flags(text, deceased=packet["patient"]["deceased"]) if text else []
    excluded = packet["meta"]["excluded_counts"]
    result = PatientResult(
        uuid=patient_id,
        http_status=status,
        summary_status=summary["status"],
        summary_reason=summary["reason"],
        counts=(len(packet["conditions"]), len(packet["medications"]), len(packet["allergies"])),
        excluded=(excluded["conditions"], excluded["medications"], excluded["allergies"]),
        wall_ms=wall_ms,
        checks=checks,
        text_violation=str(violation) if violation else None,
        flags=flags,
        accounts=accounts,
        cached_before=summary["status"] == "generated" and first_llm_ms == 0,
        problem=problem,
    )
    return result, packet


def result_row(result: PatientResult) -> dict:
    """One line of the saved results: what was measured, never the summary's words."""
    checks = result.checks
    return {
        "uuid": result.uuid,
        "http_status": result.http_status,
        "summary_status": result.summary_status,
        "summary_reason": result.summary_reason,
        "counts": list(result.counts),
        "excluded": list(result.excluded),
        "wall_ms": result.wall_ms,
        "cached_before": result.cached_before,
        "problem": result.problem,
        "flags": result.flags,
        "text_violation": result.text_violation,
        "sources": {
            "checked": len(checks),
            "resolved": sum(c.resolved for c in checks),
            "subject_ok": sum(c.subject_ok for c in checks),
            "status_ok": sum(c.status_ok is True for c in checks),
            "display_ok": sum(c.display_ok is True for c in checks),
        },
        "failed_sources": [
            c.claim.source
            for c in checks
            if not (c.resolved and c.subject_ok and c.status_ok is not False)
            or c.display_ok is False
        ],
        "accounts": [
            {"section": a.section, "hapi_total": a.hapi_total, "accounted": a.accounted}
            for a in result.accounts
        ],
    }


# ------------------------------------------------------------------------------ printing


def _flag(value: bool | None) -> str:
    return "-" if value is None else "Y" if value else "N"


def _describe(packet: dict) -> str:
    patient = packet["patient"]
    state = "deceased" if patient["deceased"] else "living"
    return f"{patient['age_years']}-year-old {patient['gender']}, {state}"


def print_patient(index: int, total: int, result: PatientResult, packet: dict, show: bool) -> None:
    meta, summary = packet["meta"], packet["summary"]
    invalid = meta["invalid_counts"]
    excluded = meta["excluded_counts"]
    timings = meta["timings_ms"] or {}
    print(f"\n==== {index}/{total}  {packet['patient']['source']}  ({_describe(packet)})")
    reason = f" ({summary['reason']})" if summary["reason"] else ""
    print(f"summary: {summary['status']}{reason}  model={summary['model']}")
    if show:
        print(f"  {summary['text']}")
    print(
        f"lists: conditions {result.counts[0]}, medications {result.counts[1]}, "
        f"allergies {result.counts[2]} | excluded {excluded['conditions']}/"
        f"{excluded['medications']}/{excluded['allergies']} | invalid {invalid['conditions']}/"
        f"{invalid['medications']}/{invalid['allergies']} | truncated {meta['truncated']}"
    )
    print(
        f"timing: fhir {timings.get('fhir')} ms, llm {timings.get('llm')} ms, "
        f"total {timings.get('total')} ms; time until a summary was ready {result.wall_ms} ms"
        + ("  (the service already had it)" if result.cached_before else "")
    )
    if result.accounts:
        parts = [f"{a.section} {a.accounted}/{a.hapi_total}" for a in result.accounts]
        verdict = "all accounted for" if all(a.ok for a in result.accounts) else "MISMATCH"
        print(f"completeness (packet accounts for / HAPI holds): {', '.join(parts)} -> {verdict}")
    checks = result.checks
    print(
        f"sources: {sum(c.resolved for c in checks)}/{len(checks)} resolved, "
        f"{sum(c.subject_ok for c in checks)}/{len(checks)} this patient's, "
        f"{sum(c.status_ok is True for c in checks)}/"
        f"{sum(c.status_ok is not None for c in checks)} status matches, "
        f"{sum(c.display_ok is True for c in checks)}/"
        f"{sum(c.display_ok is not None for c in checks)} text is the record's"
    )
    print("  found  theirs  status  text   source  (claimed status, display)")
    for check in checks:
        claim = check.claim
        detail = f"  ({claim.status}, {claim.label})" if claim.kind != "patient" else ""
        print(
            f"    {_flag(check.resolved)}      {_flag(check.subject_ok)}       "
            f"{_flag(check.status_ok)}      {_flag(check.display_ok)}    {claim.source}{detail}"
        )
    if result.text_violation:
        print(f"  !! the returned summary breaks a word rule: {result.text_violation}")
    if result.flags:
        print(f"  ?? worth a close read: {', '.join(result.flags)}")
    if result.problem:
        print(f"  !! could not finish: {result.problem}")


def _rate(part: int, whole: int) -> str:
    return f"{part}/{whole} ({part / whole:.1%})" if whole else "0/0"


def _seconds(ms: float | None) -> str:
    return "n/a" if ms is None else f"{ms / 1000:.1f} s"


def _interval(part: int, whole: int) -> str:
    interval = wilson_interval(part, whole)
    return "" if interval is None else f", 95% range {interval[0]:.0%}-{interval[1]:.0%}"


def _print_checks(batch: BatchSummary) -> None:
    print(
        f"sources: resolved {_rate(batch.resolved, batch.sources_checked)}, this patient's "
        f"{_rate(batch.subject_ok, batch.sources_checked)}, status matches "
        f"{_rate(batch.status_ok, batch.status_checked)}, text is the record's "
        f"{_rate(batch.display_ok, batch.display_checked)}"
    )
    print(
        f"completeness: every record accounted for in "
        f"{_rate(batch.completeness_ok, batch.completeness_checked)} lists"
        + (f"; patients with a mismatch: {batch.incomplete}" if batch.incomplete else "")
    )
    print(f"returned summaries that break a word rule: {batch.text_violations or 'none'}")
    if batch.problems:
        print(f"patients the script could not finish: {batch.problems}")


def print_batch(batch: BatchSummary) -> None:
    print(f"\npatients: {batch.patients}   no packet at all: {batch.not_answered}")
    reasons = ", ".join(f"{r} {n}" for r, n in sorted(batch.unavailable_by_reason.items()))
    usable = "n/a" if batch.usable_rate is None else f"{batch.usable_rate:.1%}"
    print(
        f"summaries: generated {batch.generated} ({usable} of patients with a packet); "
        f"unavailable: {reasons or 'none'}"
    )
    rate = batch.schema_valid_rate
    print(
        f"schema-valid: {_rate(batch.schema_valid, batch.answered_by_model)} of the summaries the "
        f"model answered{_interval(batch.schema_valid, batch.answered_by_model)} "
        f"(bar {SCHEMA_VALID_BAR:.0%}): "
        f"{'PASS' if rate is not None and rate >= SCHEMA_VALID_BAR else 'FAIL'}"
    )
    _print_checks(batch)
    print(
        f"time until a summary was ready (generated, not remembered): p50 "
        f"{_seconds(batch.latency_p50_ms)}, p95 {_seconds(batch.latency_p95_ms)}"
        + (
            f"; {batch.cached_before} left out because the service already had them"
            if batch.cached_before
            else ""
        )
    )
    for section, share in batch.empty_rate.items():
        print(
            f"{section}: empty for {share:.0%} of patients; records excluded as inactive: "
            f"p50 {batch.excluded_p50[section]}, p95 {batch.excluded_p95[section]}, "
            f"max {batch.excluded_max[section]}"
        )
    print_flagged(batch)


def print_flagged(batch: BatchSummary) -> None:
    if not batch.flagged:
        print("wording flags (for a person to read, not scored): none")
        return
    print("wording flags (for a person to read, not scored):")
    for flag, uuids in sorted(batch.flagged.items()):
        print(f"  {flag}: {len(uuids)}  e.g. python scripts/eval_ten.py {uuids[0]} --show-summary")
        print(f"    all: {' '.join(uuids)}")


def print_ten_totals(batch: BatchSummary) -> None:
    print(f"\ntotals: {batch.patients} patients")
    _print_checks(batch)
    reasons = batch.unavailable_by_reason or "none"
    print(f"summaries: generated {batch.generated}, unavailable {reasons}")
    if batch.flag_counts:
        print(f"wording flags (for a person to read, not scored): {batch.flag_counts}")
    print("fairness is not scored here: read each summary against its facts and mark it by hand.")


# ------------------------------------------------------------------------------ main


def _everything_checked_out(batch: BatchSummary, *, batch_mode: bool) -> bool:
    ok = batch.not_answered == 0 and not batch.problems and not batch.text_violations
    ok = ok and batch.resolved == batch.subject_ok == batch.sources_checked
    ok = (
        ok and batch.status_ok == batch.status_checked and batch.display_ok == batch.display_checked
    )
    ok = ok and batch.completeness_ok == batch.completeness_checked
    if batch_mode:
        ok = ok and (batch.schema_valid_rate or 0) >= SCHEMA_VALID_BAR
    return ok


def main(argv: list[str] | None = None, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ids", nargs="*", help="Synthea UUIDs or HAPI Patient ids")
    parser.add_argument("--file", help="a file of Synthea UUIDs, one per line (# comments allowed)")
    parser.add_argument("--limit", type=int, help="use only the first N ids from --file")
    parser.add_argument("--batch", type=int, metavar="N", help="sample N loaded patients")
    parser.add_argument("--seed", type=int, default=1, help="seed for --batch (default 1)")
    parser.add_argument("--list", action="store_true", help="with --batch: print the sample, stop")
    parser.add_argument("--out", help="save one line of results per patient to this file")
    parser.add_argument("--show-summary", action="store_true", help="print each summary's text")
    parser.add_argument(
        "--patience",
        type=float,
        default=120,
        metavar="SECONDS",
        help="ask again for a timed-out summary for this long (default 120)",
    )
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--fhir", help="HAPI base URL (default: FHIR_BASE_URL from .env)")
    args = parser.parse_args(argv)

    settings = settings or Settings()
    fhir = (args.fhir or settings.fhir_base_url).rstrip("/")
    api = args.api.rstrip("/")

    if args.batch:
        files = bundle_files(settings.seed_data_dir)
        if not files:
            print(f"no bundles in {settings.seed_data_dir}; run scripts/download_synthea.py first")
            return 2
        ids = sample_uuids(files, args.batch, args.seed)
        if args.list:
            print("\n".join(ids))
            return 0
        print(f"batch: {len(ids)} patients sampled with seed {args.seed} from {len(files)} files")
    else:
        ids = list(args.ids)
        if args.file:
            file_ids = read_priority(Path(args.file))
            ids += file_ids[: args.limit] if args.limit else file_ids
        if not ids:
            parser.error("give ids, --file, or --batch N")

    results: list[PatientResult] = []
    out = open(args.out, "w") if args.out else None  # noqa: SIM115 - closed in the finally below
    interrupted = False
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            for index, patient_id in enumerate(ids, start=1):
                try:
                    result, packet = evaluate(
                        client, api, fhir, patient_id, patience_s=args.patience
                    )
                except KeyboardInterrupt:
                    interrupted = True
                    print("\ninterrupted: summarizing what finished")
                    break
                results.append(result)
                if out:
                    out.write(json.dumps(result_row(result)) + "\n")
                    out.flush()
                if args.batch:
                    outcome = result.summary_status or f"HTTP {result.http_status}"
                    note = f" PROBLEM: {result.problem}" if result.problem else ""
                    print(f"  {index}/{len(ids)} {outcome} {result.wall_ms} ms{note}", flush=True)
                elif packet is None:
                    print(f"\n==== {index}/{len(ids)}  {patient_id}: HTTP {result.http_status}")
                else:
                    print_patient(index, len(ids), result, packet, args.show_summary)
    finally:
        if out:
            out.close()

    batch = summarize_batch(results)
    print_batch(batch) if args.batch else print_ten_totals(batch)
    if results and batch.not_answered == len(results):
        print("no packet came back at all: is the stack running?")
        return 2
    if interrupted:
        return 130
    return 0 if _everything_checked_out(batch, batch_mode=bool(args.batch)) else 1


if __name__ == "__main__":
    sys.exit(main())
