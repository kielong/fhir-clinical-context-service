#!/usr/bin/env bash
# ORIGIN: AI — script typed by Claude Code, reviewed by Kiel.
#
# Deletes EVERYTHING the deployment made by deleting its resource group: the VM, its disk, the
# address, the network. That is the only way to stop all charges (the daily auto-shutdown stops
# compute only). It asks you to type the group's name first, and calls nothing before you do.
#
#   azure/teardown.sh                              # asks, then deletes rg-clinical-context
#   azure/teardown.sh --resource-group NAME
#   azure/teardown.sh --dry-run                    # show the command only
set -euo pipefail

RG="rg-clinical-context"
DRY_RUN=0
YES=0
while (($#)); do
  case "$1" in
    --resource-group) RG="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --yes) YES=1; shift ;;
    *) echo "usage: azure/teardown.sh [--resource-group NAME] [--dry-run] [--yes]" >&2; exit 2 ;;
  esac
done

if ((DRY_RUN)); then
  echo "[dry-run] az group delete --name $RG --yes --no-wait"
  exit 0
fi

if ((!YES)); then
  echo "This deletes the resource group '$RG' and everything in it, permanently."
  printf "Type the group name to confirm: "
  read -r answer || answer=""
  [[ "$answer" == "$RG" ]] || { echo "not deleted."; exit 1; }
fi

az group delete --name "$RG" --yes --no-wait
echo "deletion started; check with: az group exists --name $RG"
