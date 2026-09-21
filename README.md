<!-- ORIGIN: AI — drafted by Claude Code from the measured results and the decisions recorded in the
     code comments; the decisions and fairness marks are Kiel's. -->

# Clinical Context Packet Service

Given a patient, the service returns a **packet**: their conditions, medications and allergies read
from a FHIR server, **each with the FHIR resource it came from**, what is missing, and a
two-sentence summary a reviewer can scan. The facts are assembled in code. A small local model
(Ollama, `gemma3:4b`, CPU only) writes **only the summary**, and its answer is checked before anyone
sees it. Nothing here approves or denies care; it gathers evidence for a human to check.

**Built:** HAPI FHIR on Postgres holding all 1,180 Synthea patients, one endpoint, the local
summary, and a one-page reviewer view. **I would improve first:** on busy charts the summary can
name a few conditions as if they were all of them. It is the one place the evaluation missed its own
bar (below).

## How to run

Needs Docker with about 10 GiB of memory (built on 9.7 GiB) and about 15 GB of disk. Python 3.12 is
only needed for the helper scripts and tests.

```bash
cp .env.example .env                           # no secrets; a local synthetic-data demo
docker compose up --build -d                   # HAPI + Postgres + Ollama + the API; pulls gemma3:4b (3.3 GB)
make install                                   # a Python 3.12 virtualenv for the scripts
.venv/bin/python scripts/download_synthea.py   # the pinned 1K sample (85 MB), checksum-verified
.venv/bin/python scripts/seed_hapi.py          # all 1,180 patients in about 9 minutes; priority ones first
make smoke                                     # /health, then one real packet

open http://localhost:8000                     # the reviewer page: load a patient, click any source
curl -s localhost:8000/v1/patients/2fa15bc7-8866-461a-9000-f739e425860a/clinical-context | python3 -m json.tool
```

- **The reviewer page** is at `localhost:8000`. Type a patient id, or click one of the examples, and
  it shows the facts (blue, from the FHIR record), the summary (orange, from the model, labeled as
  not from the record), what is missing, and a link on every fact that opens the record in HAPI.
  It is one HTML file with no build step, and it makes no decision. A page address like
  `localhost:8000/#2fa15bc7-8866-461a-9000-f739e425860a` loads that patient. The source links use
  `PUBLIC_FHIR_BASE_URL` from `.env` (`http://localhost:8080/fhir` by default); change it if HAPI is
  reached at another address.
- The endpoint is `GET /v1/patients/{id}/clinical-context`, where the id is a HAPI id or an
  identifier such as the Synthea UUID above. Interactive docs are at `localhost:8000/docs`; HAPI's
  own web interface, for checking a source by hand, is at `localhost:8080`. Real example packets
  (a deceased patient, an empty chart, a truncated list) are in [`examples/`](examples/).
- The first start is slow: HAPI is healthy after a minute or two and the model takes about 15–20 s
  to load. The seed can be stopped and restarted without duplicating anything.
- `make test` runs 622 tests (no network, model or Azure account needed); `make lint` checks style
  and types (ruff and mypy). `make install` and the container use the exact versions in
  `requirements.lock`; `make lock` re-pins them.
- `AS_OF_DATE=2019-09-16` in `.env` matters: the Synthea sample is frozen at that date, so ages
  against today would be wrong (the example patient is 73 in the data, 80 today). Leave it unset
  against a real EHR.

### Deploy to Azure (optional)

The same compose runs on one Azure VM, with the data restored from a dump instead of loaded again:

```bash
azure/make-dump.sh                             # a 150 MB dump of the running local database
azure/deploy.sh --my-ip <your-ip> --dry-run    # read every step first; it runs nothing
azure/deploy.sh --my-ip <your-ip>              # needs `az login`; only that address can connect
azure/teardown.sh                              # deletes everything it made
```

**It has never been deployed.** The template compiles and lints, 37 tests pin the safety rules, and
the dump and restore were proven here (all 527,113 resources restore in 52 s and a second HAPI
serves them correctly), but no Azure subscription was used. Steps, cost, what is exposed and why a
deploy is manual rather than automatic on every push are in [`azure/README.md`](azure/README.md).

## How it works

<img src="docs/data-flow.png" alt="Data flow: reviewer, FastAPI, HAPI FHIR, facts built in code, summary written by Ollama, checks, packet" width="680">

The facts a reviewer needs to verify come from code and can be checked against HAPI. The model only
turns them into a sentence or two, and a summary that breaks a rule is withheld. Whatever happens to
the model, the facts arrive unchanged. (Diagram source: [`docs/data-flow.mmd`](docs/data-flow.mmd).)

## Technical decisions

- **Facts in code, prose from the model.** The model never sees an id or a source and cannot write
  a fact. *Rejected:* giving the model raw FHIR and asking for the packet, where it could invent a
  source.
- **Code grouped by what it may touch.** `fhir/` talks to HAPI, `packet/` builds the facts and is
  pure (no network, no model, no clock, so it is testable with plain dicts), `llm/` is the only place
  a model is used, and the routes only wire them. The tests mirror the same folders.
- **A source on every fact; `missing` is structured.** The brief's example shows strings; here each
  gap is `{code, section, detail}`, so a program can act on it and a person can still read it.
  Allergies are included because a reviewer must not miss one.
- **Finding the patient.** Try the HAPI id, then search by identifier. The brief's example id is a
  Synthea identifier, not a HAPI id (`GET Patient/<uuid>` is a 404), so an id-only lookup would fail
  on the first example.
- **What counts as current.** Active, recurrence and relapse conditions; active and on-hold
  medications; active allergies. Everything else is *counted* as excluded, not hidden. Records that
  are marked active but plainly historical (a 1965 cardiac arrest, "History of ..." entries) are
  kept: deciding they are stale is a clinical judgment the service cannot defend.
- **Read every page, sort, then cap.** One patient has 1,275 medication requests, so the client
  follows every `next` link and a search that needs more than 50 pages is a 502, never a silent cut.
  Records are sorted newest first *before* the cap of 25. No `$everything` and no server-side status
  filter, which real EHRs do not offer uniformly.
- **A fixed `as_of` date** for ages and "current", because the dataset is frozen.
- **The summary fails closed.** The model must answer with exactly `{"summary": "..."}` (Ollama's
  structured output, re-checked), and the text must pass rules: two sentences, no identifiers, no
  approve/deny/medically-necessary language, no "stable" or "well controlled", no "none recorded"
  for a list that has entries, no numbers the record does not contain. A failure gets **one** retry
  that says what was wrong (at temperature 0 the same prompt would just repeat itself), then the
  summary is marked unavailable with a reason. The facts are returned either way. *Rejected:*
  LangChain or LangGraph around the call: 24 more packages, and they would sit between the code and
  the one pinned request that decides the output, without making a 4B model more deterministic.
- **The same packet gets the same words.** Temperature 0, top_k 1 and a fixed seed were **not
  enough**: one prompt came back worded three ways depending on Ollama's prompt-cache state. So a
  finished, checked summary is remembered, and a request waits at most 45 s for a summary before
  returning the facts; the model finishes in the background for next time.
- **A rule is enforced, not just requested.** The evaluation showed the model ignoring the
  instruction to say a patient is deceased, so code now requires it.
- **A thin reviewer page, kept out of the API.** The one endpoint is still the packet; the page only
  calls it. It builds everything from text, never markup, because display names come from the
  record; a source becomes a link only if it looks like `Type/id`; and the patient id goes in the
  URL fragment, which a browser never sends, because the server's access log prints query strings.
  *Rejected:* a front-end framework or build step, which would be more to explain than the page.
- **Azure: the same compose on one VM, with the data as a dump.** Loading 1,180 bundles over HTTP
  takes about nine minutes; the dump is 150 MB and restores in under one. Only the deployer's
  address can connect, and there is no default for it. *Rejected:* a container service or managed
  database, which would change what is being demonstrated, and deploying automatically on every push,
  which would restore a database and start a paid VM for every README edit.
- **The errors are part of the contract.** 404, 409, 422, 502 and 500 each return one fixed
  sentence and are declared in the OpenAPI (`/docs`), and a packet is sent `Cache-Control: no-store`
  so a browser or proxy does not keep a copy of a patient's record. The container installs from a
  lockfile, so a rebuild next month cannot change a version.
- **HAPI is the state.** The seed asks HAPI before loading each patient, so an interrupted load
  resumes without duplicates; HAPI runs on Postgres so data survives a restart.
- **No patient id in any log**: a keyed hash in app logs, the server's access log redacted, HAPI's
  search logging turned down.

## Results

**How it was checked.** The standard (what "fair" means, five tags for a miss, the acceptance bar)
was written and committed before any result was measured, and was not changed afterwards. Sources
were checked by script, straight against HAPI, independently of the service. **I read each of the
ten summaries next to its facts and marked it against that standard.** The words behind every mark
are saved in [`eval/`](eval/); the full write-up is [`eval/ten_patients.md`](eval/ten_patients.md).

**Ten patients read one by one** (young and old, sparse and busy, living and deceased, an empty
chart, polypharmacy, inactive records):

- **Sources are sound:** all 157 cited sources exist and belong to the right patient, all 147
  statuses match, all 147 names match the record, and every record HAPI holds for the ten is
  accounted for.
- **7 of 10 summaries fair; the bar was 9, so it was missed.** The first read scored 5 of 10. Three
  calls are close (Aaron's plural "events", Beatriz naming one of six allergies, Adam's 3 of 6
  conditions with no hedge), so the honest range is 5 to 8; the bar is missed under every reading.
- **The first failure was a real bug.** Three of four deceased patients were never called deceased.
  Floyd, deceased at 95 with 1,275 medication requests, was summarized as "recorded as active with
  ... medications such as Furosemide ... and insulin", which reads as a living patient on
  treatment. Every word copied the record faithfully, so no check on the words could catch it; it
  took a rule about the patient. After the fix all four deceased patients say so.
- **What remains:** on a long chart the model names a few conditions as if they were all of them
  (Floyd: 3 of 20, Shelly: 6 of 24, Adam: 3 of 6) and does not say "including." Dropped allergies
  are not a miss under the rule as written (Beatriz named one of six; Andreas named none).

**A batch of 100 patients nobody had looked at:** 100 of 100 summaries generated and schema-valid;
714 of 714 sources found and correct; all 300 lists accounted for; 17 of 17 deceased patients
handled correctly; time until a summary was ready 13.0 s median, 21.1 s at p95. Seven of those
patients were read by hand (none of the ten): 6 of 7 fair, one omitted. Roughly 13 of the 100
leave out a chronic condition without saying so (an estimate from word-matching; the hand-read
seven found the script's `partial_list` flag over-counts).

**Choosing the model.** Three patients, each model cold (loaded from nothing) then warm, using the
service's own prompt and checks:

| | passes the checks first try | cold / warm | tokens per second | fair (cold) |
| --- | --- | --- | --- | --- |
| llama3.2:3b | 4 of 6 | 27.1 s / 12.2 s | 6.7 | 2 of 3 |
| phi4-mini:3.8b | 4 of 6 | 21.4 s / 9.6 s | 5.3 | 1 of 3 |
| **gemma3:4b** | **6 of 6** | 26.6 s / **7.3 s** | 6.2 | 2 of 3 |

`gemma3:4b` was the only model to pass every check first try (the others ran past the length limit
on the busiest chart), was fastest when warm, and tied `llama3.2:3b` on fairness. It is a thin
basis (three patients, one run each), the standard counts only a missing *condition* as an omission
so a dropped allergy costs nothing (counted the other way `llama3.2:3b` would lead), and it is CPU
only, which is what a laptop or a plain Azure VM offers. All timings were taken on an Apple M5 with
10 CPUs and 9.7 GiB given to Docker, shared with unrelated heavy processes, so they are rougher and
slower than a quiet machine would give.

## What I would do next, and why

1. **Close the gap the evaluation found.** Have a summary say when it names only some conditions
   (for example by putting each list's size in the prompt and checking that a partial list says
   so), apply the same to allergies, and re-measure on the 100 plus more read by hand. *Why:* it is
   the one bar that failed.
2. **A check that every claim traces to a listed fact.** *Why:* the automatic checks catch
   "none recorded" and stray digits, not a partial condition list that reads as complete.
3. **Scope the packet to a request.** Redefine `missing` as the documentation still needed for
   *this* authorization request, not just what the chart lacks. *Why:* that is what a reviewer
   actually asks.
4. **A production shape on Azure.** SMART-on-FHIR or Entra ID auth, secrets in Key Vault, an audit
   trail of who looked at whom, and the model inside the payer's network so data never leaves it,
   then an Epic or Cerner connector (same R4 queries, different authentication). *Why:* none of it
   can be added to a demo without redoing the trust boundary. Not done here: the deployment
   files exist but have never been run on Azure, and CI has not yet run on GitHub.
5. **If summary latency hurts,** return the facts at once and the summary asynchronously, and try a
   larger model on a GPU, which the results suggest would help the omissions most.

Out of scope on purpose: any automatic authorization decision.
