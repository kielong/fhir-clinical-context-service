# New Forward Deployed Engineer — Technical Assignment

Source: Autonomize take-home brief, plus recruiter email from Ruth (timeline, Azure as a stretch, AI-use expectations).

## Role this is testing

FDEs take a signed customer agreement containing project scope and turn it into a working solution deployed inside a customer's cloud environment. They wire data integrations, configure AI workflows, and act as the technical face of Autonomize from project kickoff to go-live.

This assignment is a 45-minute panel with Solutions Architects, Forward Deployed Engineers, and senior technical leadership. You build a small AI-driven healthcare data service, then walk them through it.

## Problem

Before a prior-authorization request can be reviewed, someone has to pull the patient's history together out of scattered records. It is slow, manual work that Autonomize's platform is designed to automate using agentic workflows.

Build a service that assembles clinical context from a FHIR feed and shows where every fact came from. You can use any coding assistant. Cite where all code came from, human (e.g., open source) or machine (i.e., AI).

## Four required pieces

1. Run HAPI FHIR locally in Docker.
2. Load some Synthea synthetic patients into it — the sample set is 1,180 patient bundles.
3. Expose one endpoint that takes a patient and returns their packet.
4. Generate the summary with a small language model running locally.

Example payload (shape is flexible; `source` is not):

```json
{
  "patient_id": "2fa15bc7-8866-461a-9000-f739e425860a",
  "conditions": [{
    "display": "Diabetes",
    "source": "Condition/1a2b"
  }],
  "medications": [{
    "display": "Metformin 500mg",
    "source": "MedicationRequest/3c4d"
  }],
  "summary": "Two sentences a reviewer can scan.",
  "missing": ["No medications on file"]
}
```

The service assembles the evidence. It never issues a final authorization. A reviewer needs to check any line against the record.

## Model

Sized for a 3–4B model on a laptop. No GPU, no paid API.

| Model | From |
| --- | --- |
| llama3.2:3b | Meta |
| gemma3:4b | Google |
| phi4-mini:3.8b | Microsoft |
| qwen3:4b | Alibaba |

Install Ollama and use structured outputs so you get valid JSON back. An OpenAI-compatible endpoint is allowed if local hosting does not suit you. They encourage hosting via Ollama to show you can manage an LLM server and benchmark models at no cost. Expect to justify the model choice.

## Check your work

Look at ten patients yourself. For each, note whether the summary is fair and whether the sources point at something real. Put the counts and observations in the README to discuss during the panel.

## Suggested stack

Python with FastAPI, Pydantic, and Docker Compose, unless you are faster in something else. They would rather see your best work than watch you learn their preferences.

## Time box

Aim for a few hours spread across a few days. Stop at eight hours max, target under four. They would rather see a small, well-reasoned system than a large one. In the README, document what you would have done next and why — that note is worth more than another half-built feature.

## Sharing the code

Reply with a link to the repo (or attached zip) and a few sentences on what you built and what you would improve. The README covers the rest: how to run it, technical decisions, a simple visual (such as a data flow diagram), and observations on results. A compose file and a `pyproject.toml` alongside it are all they need to follow along.

## Presenting

Live during the panel, demo the project. After the demo they will open the repository, dig into specific lines of code, and ask detailed questions about where the code came from, what it does, and why it was written that way. Do not share code you cannot explain.

Questions are welcome at any point.

## Recruiter notes (Ruth)

- About a week to complete.
- If you use AI, be comfortable speaking through the code, decisions, and reasoning.
- If time allows, deploying in Azure would be a great additional touch.
- Where permitted, add thoughtful personal touches beyond the step-by-step requirements.
- The onsite panel will revolve around the take-home.
- Once reviewed, send a planned completion timeline so they can schedule the onsite shortly after.
- Once finished, send it back; Ruth shares it with the Autonomize team.
