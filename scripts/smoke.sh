#!/usr/bin/env bash
# ORIGIN: AI — script typed by Claude Code, reviewed by Kiel.
#
# Proves a running stack works end to end: /health says HAPI is up, and the example patient comes
# back as a sourced packet. Exits non-zero on any failure, so it can gate a deploy.
#
#   scripts/smoke.sh                      # against http://localhost:8000
#   scripts/smoke.sh https://my-vm:8000   # against another host
#   SMOKE_PATIENT_ID=<id> scripts/smoke.sh
set -euo pipefail

BASE="${1:-${API_URL:-http://localhost:8000}}"
PATIENT="${SMOKE_PATIENT_ID:-2fa15bc7-8866-461a-9000-f739e425860a}"

fail() {
  echo "SMOKE FAILED: $*" >&2
  exit 1
}

health=$(curl -fsS "$BASE/health") || fail "/health is not reachable at $BASE"
echo "health: $health"
python3 - "$health" <<'PY' || fail "HAPI is not reported as up"
import json, sys
sys.exit(0 if json.loads(sys.argv[1])["hapi"] == "ok" else 1)
PY

packet=$(curl -fsS "$BASE/v1/patients/$PATIENT/clinical-context") \
  || fail "the packet request failed for patient $PATIENT"
python3 - "$packet" <<'PY' || fail "the packet is missing something it must have"
import json, sys

p = json.loads(sys.argv[1])
assert p["patient"]["source"].startswith("Patient/"), "the patient has no source"
assert p["conditions"], "no conditions"
assert all(c["source"].startswith("Condition/") for c in p["conditions"]), "a condition has no source"
assert p["meta"]["as_of"], "no as_of date"
print(
    f"packet: {p['patient']['source']}, {len(p['conditions'])} conditions, "
    f"{len(p['medications'])} medications, {len(p['allergies'])} allergies, "
    f"{len(p['missing'])} gaps, summary={p['summary']['status']}"
)
PY

echo "SMOKE OK"
