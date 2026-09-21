#!/usr/bin/env bash
# ORIGIN: AI — script typed by Claude Code, reviewed by Kiel.
#
# Makes the database dump that azure/deploy.sh restores on the VM, so the 1,180 patients are not
# loaded again over HTTP (about nine minutes, against a restore). Run it on your machine with the
# stack up and the data loaded.
#
# The dump is in Postgres's compressed custom format, with no owner or privilege statements, so it
# restores under a different database password on a different machine. It is patient data, even
# though synthetic, and it is git-ignored.
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p azure/dump
docker compose ps --status running --services | grep -qx postgres \
  || { echo "error: the stack is not running (docker compose up -d)" >&2; exit 2; }

echo "dumping the database (HAPI can stay up; a dump is a consistent snapshot)..."
docker compose exec -T postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-privileges' \
  >azure/dump/hapi.dump.partial
mv azure/dump/hapi.dump.partial azure/dump/hapi.dump
ls -lh azure/dump/hapi.dump
