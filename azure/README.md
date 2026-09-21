<!-- ORIGIN: AI — runbook typed by Claude Code, reviewed by Kiel. The exposure choices it describes
     (only your address may connect, a dump instead of a re-seed, manual deploys) are Claude Code's
     proposals until Kiel adopts them. -->

# Deploying to Azure

The same `docker-compose.yml` that runs on a laptop, on one Ubuntu VM. The data arrives as a
database dump instead of being loaded again. The idea, in one sentence: *the customer's network
gets the same compose, the model stays with the data, and the data arrives as a dump, not a
re-seed.*

## Status: read this first

**Checked without an Azure account**
- The template compiles with the real Bicep compiler (0.47) and lints clean.
- 37 tests pin the rules that keep it safe: nothing is open to the world, only ports 22, 8000 and
  8080 are opened and only to your address, key-only login, a 64 GB disk, a daily shutdown, a
  random database password, the database restored *before* HAPI starts, and only committed files
  shipped. Deliberately breaking each rule (24 ways) is caught every time.
- The four scripts pass `shellcheck`, and `--dry-run` prints every step without running or calling
  anything.
- **The dump and restore were proven on this laptop.** The 3.9 GB database dumps to **150 MB in
  23 s**. It restores into a fresh database in **52 s** to exactly the same **527,113 resources**,
  with identical counts for every resource type. A second, throwaway HAPI started on the restored
  copy in 40 s and served all 1,180 patients, found the assignment patient by identifier, returned
  all 1,275 of Floyd's medication requests, and accepted a new write with a fresh id.

**Not done**
- **It has never been deployed to Azure.** No subscription was used. The first-boot Docker install,
  the VM itself, and the time a deploy takes are untested.
- CPU-only speed on the VM is unmeasured. It will differ from the numbers in the README, which
  were taken on an Apple M5; measure it on the first deploy.
- The CI job that compiles the template has not yet run on GitHub.

## What you need

- An Azure subscription, and the Azure CLI: `brew install azure-cli`, then `az login`.
- An SSH key (`ssh-keygen -t ed25519` if you have none).
- Your public IP address: `curl -s https://api.ipify.org`. It is the only address that will be
  allowed to connect, so if your address changes, deploy again with the new one.
- The stack running locally with the data loaded, to make the dump.

## Deploy

```bash
azure/make-dump.sh                                   # once: azure/dump/hapi.dump (git-ignored)
azure/deploy.sh --my-ip 203.0.113.7 --dry-run        # read every step first; runs nothing
azure/deploy.sh --my-ip 203.0.113.7                  # then for real
```

It creates the resource group and the VM, waits for first boot, copies up **one committed version**
of this repo (a `git archive` of `HEAD`, so a local `.env` or uncommitted change can never be
shipped) and the dump, runs `remote-setup.sh` on the VM, and smoke-tests it with `scripts/smoke.sh`.
On the VM the order is: start Postgres, restore the dump, then start everything else. HAPI makes
its own tables the first time it runs, so the data has to be in first.

Expect roughly 15 to 25 minutes (a guess: the VM, the Docker install, the upload, building the API
image, and pulling the 3.3 GB model). Then open `http://<vm-ip>:8000`.

Deploying again is safe: the database password is kept, and the restore is skipped if the data is
already there.

## What it creates, and what it costs

One VM (`Standard_D4s_v5`: 4 vCPU, 16 GiB, no GPU), a 64 GB Premium SSD, a static public IP, a
network and a firewall. Roughly $5 a day while running (about $0.19 an hour for the VM), plus about
$10 a month for the disk and $4 for the address. **Those are estimates; check the Azure pricing
calculator.** The VM shuts itself down every day at 20:00 UTC, which stops the compute charge but
**not** the disk or the address. To stop everything:

```bash
azure/teardown.sh          # asks you to type the resource group's name, then deletes all of it
```

## What is and is not exposed

- **Only your address** can reach the VM, on ports 22 (SSH), 8000 (the API and reviewer page) and
  8080 (HAPI, so source links work). There is no default for the address, and the script refuses
  `0.0.0.0`, `*` and any network wider than a /24.
- The model and the database are never published, so nothing can reach them from outside.
- SSH is key-only. The database password is random, made on the VM, and kept in a file only the
  deploy user can read.
- **HAPI has no login.** It accepts reads *and writes* from anyone who gets past the address
  filter, so the filter is its only protection. That is acceptable for synthetic data and a demo,
  not for anything real; see "What I would do next" in the main README.

## Why a deploy is manual, not automatic on every push

Deploying on every push to GitHub sounds tidy, but it is the wrong shape for this project:

- **A deploy here replaces state and costs money.** It restores a database and starts a paid VM.
  Every push, including a README typo, would do that.
- **It needs a trust relationship that does not exist yet.** The workflow would need a way to log
  in to your subscription (a federated identity and role assignment in Azure, plus repository
  settings), which is more to set up and more to get wrong than the deployment itself.
- **Nobody has proven the deployment works.** Automating a procedure that has never run once is
  backwards. Run `deploy.sh` by hand, fix what breaks, and only then automate.

What runs on every push already, and needs no cloud login: **CI lints and tests the code and
compiles and tests the deployment files.** If you later want a button, add a workflow that runs only
when you start it (`workflow_dispatch`), signs in with a short-lived federated identity instead of
a stored secret, and waits for approval in a protected environment. It would call this same
`deploy.sh`.

## If something goes wrong

- First boot did not finish: `ssh azureuser@<ip> 'sudo cat /var/log/cloud-init-output.log'`.
- The stack did not start: `ssh azureuser@<ip>`, then `cd /opt/clinical-context/app` and
  `docker compose ps`, `docker compose logs`.
- You cannot connect: your address probably changed. Run `deploy.sh` again with the new one.
- Start over cleanly: `azure/teardown.sh`, then deploy again.
