#!/usr/bin/env bash
# ORIGIN: AI — script typed by Claude Code, reviewed by Kiel. Runs ON the VM, started by deploy.sh.
#
# Unpacks the version deploy.sh copied up, makes a random database password (kept only in a private
# file on the VM), loads the database dump, and only then starts the whole stack. The order
# matters: HAPI creates its own tables the first time it starts, so the dump must already be in.
#
#   bash remote-setup.sh PUBLIC_IP DUMP_FILE_NAME
set -euo pipefail

PUBLIC_IP="${1:?usage: remote-setup.sh PUBLIC_IP DUMP_FILE_NAME}"
DUMP_FILE_NAME="${2:?usage: remote-setup.sh PUBLIC_IP DUMP_FILE_NAME}"
HOME_DIR="/opt/clinical-context"
cd "$HOME_DIR"

# The password is made once and kept outside the unpacked copy, so running this again (after a new
# version is copied up) does not change the password the existing database was created with.
if [ ! -f "$HOME_DIR/.env" ]; then
  tar -xzf src.tgz -O .env.example >"$HOME_DIR/.env"
  PASSWORD="$(openssl rand -hex 16)"
  sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=${PASSWORD}/" "$HOME_DIR/.env"
  sed -i "s#^PUBLIC_FHIR_BASE_URL=.*#PUBLIC_FHIR_BASE_URL=http://${PUBLIC_IP}:8080/fhir#" "$HOME_DIR/.env"
  chmod 600 "$HOME_DIR/.env"
fi

rm -rf app
mkdir app
tar -xzf src.tgz -C app
cp "$HOME_DIR/.env" app/.env
chmod 600 app/.env
cd app

set -a
# shellcheck source=/dev/null
. ./.env
set +a

docker compose up -d postgres
echo "waiting for Postgres..."
until [ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q postgres)")" = healthy ]; do
  sleep 2
done

if ! docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "select to_regclass('public.hfj_resource')" | grep -q hfj_resource; then
  echo "loading the database dump (a few minutes)..."
  docker compose exec -T postgres pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    --no-owner --no-privileges <"$HOME_DIR/$DUMP_FILE_NAME"
else
  echo "the database already has data; not restoring over it"
fi
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "select count(*) || ' resources in the database' from hfj_resource"

docker compose up --build -d
echo "waiting for the API..."
until [ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q api)")" = healthy ]; do
  sleep 3
done
docker compose ps
