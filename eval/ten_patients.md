<!-- ORIGIN: H-spec — the rubric, the tags, the acceptance bar and the choice of patients are Kiel's
     decisions; the wording was typed by Claude Code. Items marked (AI) are Claude Code's proposals
     that Kiel has not adopted yet. -->

# Evaluation: ten patients read by hand, a batch run, and a model bake-off

This is written **before** the results, so the standard cannot drift to fit them. The results are
added below the line at the end, in a later commit.

## What "fair" means

A summary is **fair** if a utilization-management reviewer reading it next to the packet's facts
would **not be misled** about what the record says. A summary is judged only against the facts in
its own packet, never against outside medical knowledge.

Every miss gets exactly one tag:

| Tag | It means |
| --- | --- |
| `invented` | states a fact the packet does not contain: a diagnosis, medication, allergy, number, date, or "none recorded" for a list that has entries |
| `omitted` | leaves out an included active chronic condition in a way that reads as complete. Naming some of many is fine when it says so ("including", "among others") |
| `stale-as-current` | presents a stopped, historical or no-longer-active record as something the patient has now |
| `overclaimed` | states control, prognosis or certainty the record does not give ("stable", "well controlled", "will") |
| `deceased-ignored` | describes a deceased patient as living or as currently on treatment, **or never says the patient is deceased** |

A summary with no tag is fair. One tag anywhere makes it unfair.

**Wording (AI).** The prompt asks the model to write "recorded as active", never "currently has".
Present tense such as "is taking metformin" for a *living* patient whose medication record is active
is what the record says, so it is **not** a miss. It is counted as `present_tense` for the whole
batch and reported, so the drift is a number and not an explanation. It does not count against the
bar. For a *deceased* patient the same wording is `deceased-ignored`.

**Unavailable summaries (AI).** A summary that is `unavailable` (timeout, model down, rejected by
the checks) is not fair, because there is nothing to be fair. It counts against the "9 of 10" bar,
which always has 10 as its denominator. A timeout is asked for again first, because the service
keeps generating after it stops waiting, so a slow machine is not punished for being slow.

## Acceptance bar

The numbers are fixed now.

| # | Bar | Measured by |
| --- | --- | --- |
| 1 | **100%** of cited sources resolve in HAPI, **and** the record belongs to this patient, **and** its status is the status the packet claims, **and** the fact's text is a name the record itself carries. **100%** of each patient's records are accounted for: what the packet shows plus what it says it left out equals what HAPI holds | `scripts/eval_ten.py`, on the ten and on the batch |
| 2 | **at least 9 of 10** summaries fair | a person, per patient |
| 3 | **zero** determination language (approve, deny, authorize, medically necessary, eligible) in any returned summary | the service's checks, re-run independently by the script, on the ten and on the batch |
| 4 | **at least 95%** of batch summaries schema-valid | `scripts/eval_ten.py --batch 100 --seed 1` |

Why these numbers. Sources and completeness are 100% because the packet is assembled in code from
the record, so any miss is a bug, not a statistic. Nine of ten allows one miss from a small model;
ten patients cannot support a tighter claim. Determination language is zero because it is the one
thing a reviewer must never be handed. The batch is 100 patients because at 50 a 95% bar fails on
the third bad summary and one unlucky sample would decide the verdict; at 100 it tolerates five.
The report prints the 95% range around the rate so the uncertainty is visible.

"Schema-valid" (AI): of the summaries the model actually answered, the share whose answer was
exactly `{"summary": <string>}`. A timeout or a model that was not running never produced an answer,
so those are reported separately and do not count for or against the model. The share of patients
who got a summary at all is reported next to it.

The sample is 100 patients drawn with seed 1 from the bundle filenames. The report states how many
were loaded and how many answered.

## How each check is done

- **Mechanical, by script.** For each patient the script fetches the packet, then reads the
  records straight from HAPI, independently of the service: every cited `source` must exist, belong
  to this patient, carry the claimed status and have the name the packet shows; and for each list,
  HAPI's own count must equal what the packet shows plus what it says it left out. It never scores
  fairness. It also flags wording worth a close read (`present_tense`, `deceased_not_stated`,
  `deceased_present_tense`) and lists which patients, so a person can go and read them.
- **By a person.** Read the summary next to the facts the script prints, mark it fair or unfair,
  give each miss one tag, and spot-check two or three sources in HAPI's own web interface. The
  script hides summary text unless `--show-summary` is passed.
- **Reading patients nobody has seen (AI).** `--batch 100 --seed 1 --list` prints the sample. Read
  the summaries of five of them that are not among the ten, so there is a check that was not tuned
  against.

```
python scripts/eval_ten.py --file eval/ten_patients.txt                  # the ten, facts and sources
python scripts/eval_ten.py --file eval/ten_patients.txt --show-summary   # ...with the text
python scripts/eval_ten.py --batch 100 --seed 1 --out eval/batch.jsonl   # the batch
python scripts/eval_ten.py --batch 100 --seed 1 --list                   # who is in the sample
```

## The ten

Chosen for variety: young and old, sparse and busy, living and deceased, an entirely empty chart,
polypharmacy, and inactive records that must be left out.

| # | Synthea UUID | HAPI id | Why this patient |
| --- | --- | --- | --- |
| 1 | `2fa15bc7-8866-461a-9000-f739e425860a` | 1000 | Aaron697 Brekke496, 73: the example patient; a 1965 cardiac arrest still marked active; no active medications |
| 2 | `0979f4fe-08c5-414e-ba1c-6ccf852bcce4` | 1319 | Floyd420 Jerde200, deceased at 95: 1,275 medication requests, only a few active |
| 3 | `9da0dcfc-05e3-4e8e-95ff-b04b56f748be` | 19807 | Shelly431 Corwin846, 84: 24 active conditions, the busiest chart |
| 4 | `5919de03-6363-41a7-b251-f5be75149adc` | 22089 | Jose871 Williamson769, deceased at 93: 14 active medications |
| 5 | `e2129449-9c68-4155-a826-e22091aa4742` | 22693 | Alicia629 Walter473, 2: a child with an empty chart |
| 6 | `f7f63ca8-d282-4520-9a68-3177e2a5db6f` | 22817 | Andreas188 Dare640, 23: five active allergies and one inactive |
| 7 | `9dc305b0-c821-49f3-817c-58e853bce8b1` | 23227 | Beatriz277 Salas880, 25: the most allergies (6 active, 4 inactive) |
| 8 | `9e2653fc-49e0-4b2e-86f8-e664bbe07be3` | 23751 | Adam631 Shields502, deceased at 14: seven allergies |
| 9 | `20123c13-40e7-4134-8a18-58c57be98c74` | 24215 | Alan320 Wiza601, 4: a young child, nearly empty chart |
| 10 | `2c57a897-8381-44a6-920f-e074fa6f74cf` | 24405 | Lorenzo669 Cuellar188, deceased at 69: 192 medication requests; "finding" and "situation" records marked active |

Things to look at with your own eyes rather than assume: the 1965 `Cardiac Arrest` for #1, the
deceased patients whose medications are still "active" (#2, #4, #10), whether each deceased
patient's summary says so (#2, #4, #8, #10), and the "History of ..." records marked active for #10.

## The model bake-off

`scripts/bakeoff.py` runs each candidate (`llama3.2:3b`, `phi4-mini:3.8b`, `gemma3:4b`) on three
patients (#1 sparse with a stale record, #4 deceased with many medications, #6 an inactive allergy
among active ones). For each it unloads the model, asks once (cold, so loading is included) and
asks again (warm), using the service's own prompt, request and answer checks, and records: JSON
valid, passes the checks, cold and warm time, tokens per second, and whether cold and warm gave the
same words. It measures in the same Docker container the service uses, on CPU, because the
deployment target is a CPU VM; the report states the machine, the RAM given to Docker, and what
Ollama itself says about where the model ran. Fairness of the words is marked by a person against
the rubric above. Temperature stays 0.

## How this test was set up

The prompt and the answer checks were built and tuned around these same patients, so their
summaries were read while the system was being developed. This is therefore not a blind test of
the model's wording. What it does establish is how the **final** model, prompt and checks behave
against a standard written down before the final run. The batch of 100 (nearly all patients nobody
looked at) shows whether the ten were typical, and the five unseen patients read by hand are the
part of the wording check that was not tuned against.

---

<!-- Results are added below in a later commit. -->
