#!/usr/bin/env bash
# ORIGIN: AI — script typed by Claude Code, reviewed by Kiel. The choices (only your address may
#   connect; the data arrives as a dump; one committed version is shipped, never your .env) are
#   Claude Code's proposals until Kiel adopts them. Never run against Azure by its author.
#
# Deploys the stack to one Azure VM: creates the resource group and the VM (azure/main.bicep),
# waits for first boot, copies up ONE COMMITTED VERSION of this repo and the database dump, then
# runs azure/remote-setup.sh on the VM and smoke-tests it.
#
#   azure/make-dump.sh                              # once, on your machine, with the stack running
#   azure/deploy.sh --my-ip 203.0.113.7 --dry-run   # read every step without running any
#   azure/deploy.sh --my-ip 203.0.113.7             # for real (needs `az login`)
#
# Every check that can refuse (the address, the dump, the key) happens BEFORE the first cloud call.
set -euo pipefail

usage() {
  cat <<'USAGE'
usage: azure/deploy.sh --my-ip ADDRESS [options]

  --my-ip ADDRESS        REQUIRED. The one IPv4 address, or a network of /24 or smaller, that may
                         connect (ports 22, 8000, 8080). Find yours: curl -s https://api.ipify.org
  --resource-group NAME  default: rg-clinical-context
  --location REGION      default: eastus
  --vm-size SIZE         default: Standard_D4s_v5 (4 vCPU, 16 GiB; no GPU)
  --ssh-public-key FILE  default: ~/.ssh/id_ed25519.pub
  --dump FILE            default: azure/dump/hapi.dump (make it with azure/make-dump.sh)
  --dry-run              print every command, run none, and call no cloud service
USAGE
}

die() {
  echo "error: $*" >&2
  exit 2
}

RG="rg-clinical-context"
LOCATION="eastus"
VM_SIZE="Standard_D4s_v5"
SSH_PUB="$HOME/.ssh/id_ed25519.pub"
DUMP="azure/dump/hapi.dump"
MY_IP=""
DRY_RUN=0

while (($#)); do
  case "$1" in
    --my-ip) MY_IP="${2:-}"; shift 2 ;;
    --resource-group) RG="${2:-}"; shift 2 ;;
    --location) LOCATION="${2:-}"; shift 2 ;;
    --vm-size) VM_SIZE="${2:-}"; shift 2 ;;
    --ssh-public-key) SSH_PUB="${2:-}"; shift 2 ;;
    --dump) DUMP="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done

[[ -n "$MY_IP" ]] || { usage >&2; die "--my-ip is required: say which address may connect"; }

# One IPv4 address, or a network of /24 to /32. Never 0.0.0.0, never a wide network.
valid_address() {
  local value="$1" ip="${1%%/*}" prefix=32 octet
  [[ "$value" == */* ]] && prefix="${value#*/}"
  [[ "$ip" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
  for octet in "${BASH_REMATCH[@]:1}"; do
    ((10#$octet <= 255)) || return 1
  done
  [[ "$prefix" =~ ^[0-9]{1,2}$ ]] || return 1
  ((10#$prefix >= 24 && 10#$prefix <= 32)) || return 1
  [[ "$ip" != "0.0.0.0" ]]
}
valid_address "$MY_IP" || die "'$MY_IP' is not an acceptable address: give one IPv4 address, or a network of /24 or smaller (never 0.0.0.0 or a wide range)"

CIDR="$MY_IP"
[[ "$CIDR" == */* ]] || CIDR="$MY_IP/32"

if ((!DRY_RUN)); then
  [[ -f "$DUMP" ]] || die "the database dump '$DUMP' does not exist: run azure/make-dump.sh first"
  [[ -f "$SSH_PUB" ]] || die "no SSH public key at '$SSH_PUB': create one (ssh-keygen -t ed25519) or pass --ssh-public-key"
  command -v az >/dev/null || die "the Azure CLI (az) is not installed"
  az account show >/dev/null 2>&1 || die "not logged in to Azure: run 'az login'"
  command -v git >/dev/null || die "git is required"
fi

# Show a command; run it unless this is a dry run.
run() {
  if ((DRY_RUN)); then
    printf '[dry-run]'
    printf ' %q' "$@"
    echo
  else
    "$@"
  fi
}
step() { echo; echo "==> $*"; }

COMMIT="$(git rev-parse --short HEAD)"
if ! git diff --quiet HEAD 2>/dev/null; then
  echo "warning: you have uncommitted changes. Only the committed version ($COMMIT) is deployed." >&2
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o ServerAliveInterval=30)
ADMIN="azureuser"
REMOTE_DIR="/opt/clinical-context"

step "1. Resource group '$RG' in $LOCATION"
run az group create --name "$RG" --location "$LOCATION" --output none

step "2. The VM ($VM_SIZE), reachable only from $CIDR"
if ((DRY_RUN)); then
  KEY_ARG="sshPublicKey=<contents of $SSH_PUB>"
else
  KEY_ARG="sshPublicKey=$(cat "$SSH_PUB")"
fi
run az deployment group create --resource-group "$RG" --template-file azure/main.bicep \
  --parameters "allowedSourceIp=$CIDR" "vmSize=$VM_SIZE" "$KEY_ARG" --output none
if ((DRY_RUN)); then
  IP="<vm-ip>"
else
  IP="$(az deployment group show --resource-group "$RG" --name main --query properties.outputs.publicIp.value -o tsv)"
fi
echo "VM address: $IP"

step "3. Wait for first boot (Docker is installed by cloud-init)"
if ((DRY_RUN)); then
  echo "[dry-run] ssh $ADMIN@$IP cloud-init status --wait   (retried for up to 10 minutes)"
else
  for _ in $(seq 1 60); do
    if ssh "${SSH_OPTS[@]}" "$ADMIN@$IP" 'cloud-init status --wait' >/dev/null 2>&1; then
      READY=1
      break
    fi
    sleep 10
  done
  [[ "${READY:-0}" == 1 ]] || die "the VM did not finish its first boot; look at /var/log/cloud-init-output.log on $IP"
fi

step "4. Ship commit $COMMIT: only what git tracks, so your .env can never be copied"
run git archive --format=tar.gz --output "$WORK/src.tgz" HEAD
run scp "${SSH_OPTS[@]}" "$WORK/src.tgz" azure/remote-setup.sh "$DUMP" "$ADMIN@$IP:$REMOTE_DIR/"

step "5. Restore the database, then start the stack (runs on the VM)"
run ssh "${SSH_OPTS[@]}" "$ADMIN@$IP" "cd $REMOTE_DIR && bash remote-setup.sh $IP $(basename "$DUMP")"

step "6. Smoke test from here"
run scripts/smoke.sh "http://$IP:8000"

step "Done"
cat <<DONE
  Reviewer page:  http://$IP:8000
  HAPI:           http://$IP:8080/fhir   (NO login: the address filter is its only protection)
  Stop billing:   azure/teardown.sh --resource-group $RG
  The VM shuts itself down daily; that stops compute charges, not the disk or the address.
DONE
