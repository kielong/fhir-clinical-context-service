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


# Results

**Every fairness mark below was made by Claude Code (the AI that wrote the system), reading each
summary against its facts and the rubric above. They are not yet confirmed by Kiel, and the
author of a system is not an independent judge.** The words behind every mark are in this
directory so anyone can check them: `bakeoff.json`, `ten_before.txt`, `ten_after.txt`,
`batch.jsonl` (measurements, never the summaries' words).

The rubric above was not changed after any result was seen. Where it turned out to be silent or
blunt, that is said below instead of being edited away.

Environment for every number: Docker Desktop on an Apple M5 (10 CPUs and 9.7 GiB given to Docker),
CPU only, Ollama 0.34.2 in the compose container, temperature 0, top_k 1, seed 0. **The machine was
shared with unrelated heavy processes (load average between 10 and 75 on a 10-CPU machine for much
of the ten and batch runs), so every timing here is slower and noisier than a quiet machine would
give.** Load was not recorded during the bake-off.

## Bake-off: three models, three patients

Aaron (sparse, a stale 1965 record), Jose (deceased, 10 conditions and 14 medications) and Andreas
(living, five allergies). Each model cold (loaded from nothing), then warm, using the service's own
prompt, request and checks.

| | llama3.2:3b | phi4-mini:3.8b | gemma3:4b |
| --- | --- | --- | --- |
| Valid JSON | 6/6 | 6/6 | 6/6 |
| Passes the service's answer checks | 4/6 | 4/6 | **6/6** |
| Cold time, median | 27.1 s | 21.4 s | 26.6 s |
| Warm time, median | 12.2 s | 9.6 s | **7.3 s** |
| Tokens per second | 6.7 | 5.3 | 6.2 |
| Same words cold and warm | 2/3 | 1/3 | 2/3 |
| Fair (cold run, Claude Code's marks) | 2/3 | 1/3 | 2/3 |

- Both `llama3.2:3b` and `phi4-mini:3.8b` failed the length limit on Jose, cold and warm: a
  93-year-old with 24 records makes them list everything. `gemma3:4b` was the only model to pass
  every check on its first attempt, so it needs no retry.
- Marks: Aaron was fair for all three. Jose was unfair for all three (`deceased-ignored`, and in
  addition `invented` for `phi4-mini:3.8b`, which called active medications "on hold", and
  `omitted` for `gemma3:4b`). Andreas was fair for `llama3.2:3b`, unfair for `phi4-mini:3.8b`
  (`omitted`: it dropped the rhinitis and both medications), and fair for `gemma3:4b` under the
  rubric as written.
- **The rubric is silent on allergies.** `omitted` covers only conditions, so `gemma3:4b` dropping
  all five of Andreas's allergies is not counted. If it were, `gemma3:4b` would be 1/3 and
  `llama3.2:3b` would lead. Read both ways, `phi4-mini:3.8b` is last.
- Identical prompts came back worded differently between cold and warm for every model. That is
  why the service remembers a finished summary rather than relying on the model repeating itself.
- **Locked: `gemma3:4b`.** It passed every answer check first try, had the fastest warm time, and
  tied `llama3.2:3b` on fairness under the rubric as written. Three patients and one run each is a
  thin basis, and the rubric reading above could reverse the fairness tie.

## The ten, before and after one fix

The first read used the system as committed (`gemma3:4b`). It failed the bar, and the failures had
two causes. Three of the four deceased patients were never called deceased, one described as "is
taking medications", although the prompt asks for it. Three summaries named some of a long list of
conditions as if it were all of them.

The fix, made once and then measured (not tuned further): a check that a deceased patient's
summary must say so and must not say they are currently on treatment (a failing answer is retried,
then withheld); the prompt now asks for the word "deceased" in the first sentence, past tense for
the rest, plus a reminder at the very end of the facts for deceased patients only, where a small
model pays attention; and a prompt rule to say "including" when a list is partial.

| # | Patient | Before | After |
| --- | --- | --- | --- |
| 1 | Aaron, 73 | fair | fair |
| 2 | Floyd, deceased | unfair: `deceased-ignored` | unfair: `omitted` (names 3 of 20 conditions, no "including") |
| 3 | Shelly, 84 | unfair: `omitted` | unfair: `omitted` (6 of 24) |
| 4 | Jose, deceased | unfair: `deceased-ignored`, `omitted` | fair |
| 5 | Alicia, 2 | fair | fair |
| 6 | Andreas, 23 | fair (allergies dropped, not counted) | fair (same) |
| 7 | Beatriz, 25 | fair | unfair: `invented` ("a low criticality allergy to bee venom" reads as her only allergy; she has six) |
| 8 | Adam, deceased at 14 | unfair: `omitted` | unfair: `omitted` (3 of 6) |
| 9 | Alan, 4 | fair | fair |
| 10 | Lorenzo, deceased | unfair: `deceased-ignored` | fair |
| | **Fair** | **5 of 10** | **6 of 10** |

The wording of individual summaries changed between runs because the prompt changed and the model
is not perfectly repeatable, so compare the tallies and tags, not single rows.

- Deceased patients: 3 of 4 never said so before; **4 of 4 say so after**, and none is described as
  currently on treatment. Across the batch below, **17 of 17 deceased patients** were handled
  correctly and none went unavailable.
- What remains is `omitted`: on a busy chart a two-sentence summary cannot name everything, and this
  model often does not say so. The prompt rule did not fix it.
- Under a stricter reading that also counts dropped allergies, the "after" tally is 5 of 10 (Andreas
  becomes unfair).

### The unfair example in detail

Floyd, deceased at 95, 20 active conditions and 1,275 medication requests. His first summary read:
"The patient is recorded as active with chronic congestive heart failure, Alzheimer's disease, and
a history of multiple conditions including stroke, diabetes, and hyperlipidemia. The patient is
also recorded as active with medications such as Furosemide, Simvistatin, and insulin." A reviewer
reading only that would take him for a living patient on treatment. Nothing in it is false as a
copy of the record's statuses, which is exactly why a word check could not catch it: it needed a
rule about the patient, not about the words. After the fix the same patient's summary begins "The
deceased 95-year-old male was recorded as having...", in the past tense. It is still unfair, for a
different reason: it names 3 of his 20 conditions as if that were all of them.

## The batch: 100 patients nobody had looked at

`--batch 100 --seed 1`: 100 of the 1,180 loaded patients, none of them among the ten.

| | Result |
| --- | --- |
| Summaries generated | 100 of 100 (no packet missing, none unavailable) |
| Schema-valid | 100 of 100 (95% range 96%-100%) |
| Sources resolved, and the patient's | 714 of 714 |
| Status matches | 614 of 614 |
| Fact text is the record's own | 614 of 614 |
| Every record accounted for (shown + left out = what HAPI holds) | 300 of 300 lists |
| Returned summaries breaking a word rule (includes determination language) | 0 |
| Time until a summary was ready (generated), p50 / p95 | 13.0 s / 21.1 s (on a loaded machine) |
| Lists empty | conditions 14%, medications 36%, allergies 86% |
| Records excluded as not active, p50 / p95 / max | conditions 3 / 8 / 12, medications 3 / 19 / 122 |
| Deceased patients | 17, with no wording flag (`deceased_not_stated` or `deceased_present_tense`) |
| Present-tense treatment wording (`present_tense`, not scored) | 30 of 100 |
| Named some conditions without saying there were more (`partial_list`) | 37 of the 46 charts with four or more conditions |

The `partial_list` flag was added after the ten had been run, so it appears only in the batch. It is
crude and over-counts: it fires on any chart with four or more
conditions whose summary has no "including"-style word, even when the summary names every one.
Reading the first four flagged patients, three were complete and one was a real miss. Checking each
flagged summary against its conditions by word-matching (an estimate; only one was read by hand),
about **13 of the 100** omit a chronic condition without saying so.

Seven patients from the batch were read by hand (the first five in the sample, plus two flagged
ones): six were fair and one unfair (`omitted`). The first five happened to be small charts, which
the model handles well; they do not test the busy-chart weakness.

## Against the bar

| # | Bar | Result |
| --- | --- | --- |
| 1 | Sources correct and every record accounted for, 100% | **Met.** Ten: 157/157 sources, 147/147 statuses, 147/147 texts, 30/30 lists. Batch: 714/714, 614/614, 614/614, 300/300 |
| 2 | At least 9 of 10 fair | **Not met.** 6 of 10 (5 of 10 before the fix) |
| 3 | Zero determination language | **Met.** None in the 110 summaries returned |
| 4 | At least 95% of the batch schema-valid | **Met.** 100 of 100 |

## What this does and does not show

- The sourcing is sound: on real data nothing was misattributed, mis-statused, misnamed or dropped.
  The model is only ever asked to write the summary, and the summary is where the weakness is.
- The 9-of-10 bar fails because of `omitted` on busy charts. The ten were chosen for variety, so
  they are heavier than typical: 46 of the 100 batch patients have four or more conditions, and
  about 13 of the 100 show the omission, so on typical patients the rate is much better than on the
  ten. That estimate covers one kind of miss only; the other kinds were checked by hand on just the
  patients named above.
- The ten were tuned against during development, so they are not a blind test; the batch and the
  seven hand-read patients are the part that was not.
- One model family, three patients for the bake-off, one run each, on a machine shared with other
  work. The timings are indicative, not benchmarks.
- Still open, and not decided here: whether to require a summary to say when it lists only some
  conditions (this would cost retries and some unavailable summaries), and whether the rubric's
  `omitted` should also cover allergies. Neither was changed after seeing results.
