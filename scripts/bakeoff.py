#!/usr/bin/env python3
# ORIGIN: AI — script typed by Claude Code from the agreed bake-off method (same patients, each
#   model cold then warm, JSON validity, latency, tokens per second, environment recorded), reviewed
#   by Kiel. Claude Code's additions, not yet adopted: reusing the service's own prompt, request and
#   checks so the numbers describe the service, unloading before each cold run, recording whether
#   cold and warm gave the same words, and waiting out the service's own background generation.
"""Compare candidate models on the same patients, in the environment the service runs in.

For each model and patient: unload the model, ask (cold: includes loading it), ask again (warm).
It uses the service's own prompt, request options and answer checks, so the numbers are what the
service would see. Whether a summary is *fair* is a person's call, so the words are hidden unless
you ask for them (they are always saved to --out, for reading afterwards).

  docker compose exec ollama ollama pull phi4-mini:3.8b     # once per model
  python scripts/bakeoff.py --out eval/bakeoff.json --environment "Docker Desktop, CPU only, 10 GiB"

Exit status: 0 if it ran; 2 if a model is not pulled, a patient was not found, or Ollama is down.
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass

import httpx

from clinical_context.config import Settings
from clinical_context.llm.checks import check_summary
from clinical_context.llm.ollama import InvalidOutput, ModelUnreachable, ask
from clinical_context.llm.prompt import build_prompt
from clinical_context.packet.models import ClinicalContextPacket

DEFAULT_MODELS = ["llama3.2:3b", "phi4-mini:3.8b", "gemma3:4b"]
# Aaron697 (sparse, a stale 1965 condition), Jose871 (deceased, polypharmacy), Andreas188 (an
# inactive allergy among active ones).
DEFAULT_PATIENTS = [
    "2fa15bc7-8866-461a-9000-f739e425860a",
    "5919de03-6363-41a7-b251-f5be75149adc",
    "f7f63ca8-d282-4520-9a68-3177e2a5db6f",
]
PACKET_TIMEOUT_SECONDS = 150
WAIT_FOR_SERVICE_TRIES = 30


@dataclass
class Run:
    model: str
    patient: str
    phase: str  # "cold" or "warm"
    outcome: str  # ok, invalid_json, timeout, not_available, unreachable
    json_valid: bool
    violation: str | None  # the answer rule the words broke, if any
    total_ms: int | None
    load_ms: int | None
    prompt_tokens: int | None
    prompt_ms: int | None
    generated_tokens: int | None
    generation_ms: int | None
    tokens_per_s: float | None
    text: str | None
    same_words_as_cold: bool | None  # warm runs only: did the prompt cache change the wording?
    processor: str | None = None  # from Ollama's own report of where the model is loaded


@dataclass
class ModelRow:
    model: str
    runs: int
    json_valid: int
    checked: int  # runs that produced words to check
    passes_checks: int
    cold_ms_median: float | None
    warm_ms_median: float | None
    tokens_per_s_median: float | None
    same_words: str  # "2/3 warm runs matched the cold run"
    outcomes: dict[str, int]


def tokens_per_second(timings: dict | None) -> float | None:
    """Ollama's own token count over its own generation time."""
    if not timings:
        return None
    tokens, ms = timings.get("generated_tokens"), timings.get("generation_ms")
    if not tokens or not ms:
        return None
    return tokens / (ms / 1000)


# ------------------------------------------------------------------------------ measuring


async def unload(http: httpx.AsyncClient, settings: Settings) -> None:
    """Drop the model from memory, so the next request has to load it. Best effort."""
    body = {"model": settings.ollama_model, "messages": [], "keep_alive": 0}
    try:
        await http.post(f"{settings.ollama_host}/api/chat", json=body, timeout=60)
    except httpx.HTTPError:
        pass


async def _processor(http: httpx.AsyncClient, settings: Settings) -> str | None:
    """Where Ollama says the loaded model is running: CPU, GPU, or split."""
    try:
        response = await http.get(f"{settings.ollama_host}/api/ps", timeout=10)
        for loaded in response.json().get("models", []):
            if loaded.get("name") == settings.ollama_model:
                vram, size = loaded.get("size_vram", 0), loaded.get("size", 0)
                return "CPU" if vram == 0 else "GPU" if vram >= size else "CPU+GPU"
    except (httpx.HTTPError, ValueError):
        pass
    return None


async def _measure(
    http: httpx.AsyncClient,
    settings: Settings,
    packet: ClinicalContextPacket,
    label: str,
    phase: str,
) -> Run:
    system, user = build_prompt(packet)
    outcome, answer = "ok", None
    try:
        answer = await ask(http, settings, system, user)
    except InvalidOutput:
        outcome = "invalid_json"
    except ModelUnreachable as error:
        outcome = "not_available" if str(error) == "HTTP 404" else "unreachable"
    except httpx.TimeoutException:
        outcome = "timeout"

    timings = answer.timings if answer else None
    timings = timings or {}
    violation = check_summary(answer.summary, packet, user) if answer else None
    return Run(
        model=settings.ollama_model,
        patient=label,
        phase=phase,
        outcome=outcome,
        json_valid=answer is not None,
        violation=str(violation) if violation else None,
        total_ms=timings.get("total_ms"),
        load_ms=timings.get("load_ms"),
        prompt_tokens=timings.get("prompt_tokens"),
        prompt_ms=timings.get("prompt_ms"),
        generated_tokens=timings.get("generated_tokens"),
        generation_ms=timings.get("generation_ms"),
        tokens_per_s=tokens_per_second(timings),
        text=answer.summary if answer else None,
        same_words_as_cold=None,
    )


async def bake_model(
    http: httpx.AsyncClient, settings: Settings, packets: dict[str, dict]
) -> list[Run]:
    """Every patient, cold then warm, for the one model in `settings`."""
    runs: list[Run] = []
    try:
        for label, data in packets.items():
            packet = ClinicalContextPacket.model_validate(data)
            await unload(http, settings)  # so the cold run really includes loading
            cold = await _measure(http, settings, packet, label, "cold")
            cold.processor = await _processor(http, settings)
            runs.append(cold)
            if cold.outcome in ("not_available", "unreachable", "timeout"):
                break  # asking again, or asking about more patients, would only repeat it
            warm = await _measure(http, settings, packet, label, "warm")
            warm.same_words_as_cold = None if cold.text is None else warm.text == cold.text
            runs.append(warm)
    finally:
        await unload(http, settings)  # so it does not crowd the next model out of memory
    return runs


# ------------------------------------------------------------------------------ reporting


def _median(values) -> float | None:
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def summarize_models(runs: list[Run]) -> list[ModelRow]:
    rows = []
    for model in dict.fromkeys(r.model for r in runs):  # in the order they were run
        mine = [r for r in runs if r.model == model]
        warm = [r for r in mine if r.phase == "warm" and r.same_words_as_cold is not None]
        rows.append(
            ModelRow(
                model=model,
                runs=len(mine),
                json_valid=sum(r.json_valid for r in mine),
                checked=sum(r.text is not None for r in mine),
                passes_checks=sum(r.text is not None and r.violation is None for r in mine),
                cold_ms_median=_median(r.total_ms for r in mine if r.phase == "cold"),
                warm_ms_median=_median(r.total_ms for r in mine if r.phase == "warm"),
                tokens_per_s_median=_median(r.tokens_per_s for r in mine),
                same_words=f"{sum(bool(r.same_words_as_cold) for r in warm)}/{len(warm)}",
                outcomes=dict(Counter(r.outcome for r in mine)),
            )
        )
    return rows


def _fmt_ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value / 1000:.1f} s"


def print_table(rows: list[ModelRow]) -> None:
    print(
        f"\n{'model':<16} {'JSON ok':>8} {'passes checks':>14} {'cold':>8} {'warm':>8} "
        f"{'tok/s':>7} {'same words':>11}  outcomes"
    )
    for r in rows:
        speed = "n/a" if r.tokens_per_s_median is None else f"{r.tokens_per_s_median:.1f}"
        print(
            f"{r.model:<16} {f'{r.json_valid}/{r.runs}':>8} {f'{r.passes_checks}/{r.checked}':>14} "
            f"{_fmt_ms(r.cold_ms_median):>8} {_fmt_ms(r.warm_ms_median):>8} {speed:>7} "
            f"{r.same_words:>11}  {r.outcomes}"
        )
    print("cold = includes loading the model; warm = the same request straight after; medians.")
    print("'same words' = warm runs whose text matched the cold run of the same patient.")


# ------------------------------------------------------------------------------ main


async def available_models(http: httpx.AsyncClient, host: str) -> set[str]:
    response = await http.get(f"{host}/api/tags", timeout=10)
    response.raise_for_status()
    return {m["name"] for m in response.json().get("models", [])}


async def _fetch_packet(
    http: httpx.AsyncClient, api: str, patient_id: str, pause_s: float
) -> dict | None:
    """The packet for a patient. If the service is still generating its own summary in the
    background, wait for it: the bake-off and the service would otherwise share the one model."""
    for _ in range(WAIT_FOR_SERVICE_TRIES):
        response = await http.get(f"{api}/v1/patients/{patient_id}/clinical-context")
        if response.status_code != 200:
            return None
        packet = response.json()
        if packet["summary"]["reason"] != "timeout":
            return packet
        await asyncio.sleep(pause_s)
    return packet


async def _environment(
    http: httpx.AsyncClient, host: str, note: str | None, runs: list[Run]
) -> dict:
    version = None
    try:
        version = (await http.get(f"{host}/api/version", timeout=10)).json().get("version")
    except (httpx.HTTPError, ValueError):
        pass
    processor = next((r.processor for r in runs if r.processor), None)
    return {"note": note, "ollama_version": version, "processor": processor}


async def run_bakeoff(args: argparse.Namespace, settings: Settings) -> int:
    models = args.model or DEFAULT_MODELS
    ids = list(args.ids) or DEFAULT_PATIENTS
    host = (args.ollama or settings.ollama_host).rstrip("/")
    api = args.api.rstrip("/")
    base = with_host_and_timeout(settings, host, args.timeout)

    async with httpx.AsyncClient(timeout=PACKET_TIMEOUT_SECONDS) as http:
        try:
            present = await available_models(http, host)
        except httpx.HTTPError:
            print(f"cannot reach Ollama at {host}; is the stack running?")
            return 2
        missing = [m for m in models if m not in present]
        if missing:
            for model in missing:
                print(f"not pulled: {model}   ->  docker compose exec ollama ollama pull {model}")
            return 2

        packets: dict[str, dict] = {}
        for patient_id in ids:
            packet = await _fetch_packet(http, api, patient_id, args.pause)
            if packet is None:
                print(f"the service has no packet for {patient_id}; is it loaded and running?")
                return 2
            packets[patient_id[:8]] = packet

        runs: list[Run] = []
        for model in models:
            print(f"measuring {model} ...", flush=True)
            runs += await bake_model(http, base.model_copy(update={"ollama_model": model}), packets)
        environment = await _environment(http, host, args.environment, runs)

    print_table(summarize_models(runs))
    print(f"environment: {environment}")
    for r in runs:
        detail = r.violation or r.outcome
        print(f"  {r.model:<16} {r.patient} {r.phase:<4} {detail:<24} {_fmt_ms(r.total_ms)}")
        if args.show_text and r.text:
            print(f"      {r.text}")
    if args.out:
        with open(args.out, "w") as handle:
            json.dump(
                {
                    "measured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "environment": environment,
                    "runs": [asdict(r) for r in runs],
                },
                handle,
                indent=2,
            )
        print(f"saved to {args.out} (includes the words; read them there)")
    return 0


def with_host_and_timeout(settings: Settings, host: str, timeout: float) -> Settings:
    return settings.model_copy(update={"ollama_host": host, "ollama_timeout_seconds": timeout})


def main(argv: list[str] | None = None, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ids", nargs="*", help="Synthea UUIDs (default: the three in the plan)")
    parser.add_argument("--model", action="append", help="a model tag; repeat for several")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--ollama", help="Ollama base URL (default: OLLAMA_HOST from .env)")
    parser.add_argument("--timeout", type=float, default=300, help="seconds per request")
    parser.add_argument(
        "--pause", type=float, default=5, help="seconds between waits on the service"
    )
    parser.add_argument("--environment", help="free text: hardware, RAM given to Docker, ...")
    parser.add_argument("--out", help="save every run, with its words, to this JSON file")
    parser.add_argument("--show-text", action="store_true", help="print each summary's words")
    args = parser.parse_args(argv)
    return asyncio.run(run_bakeoff(args, settings or Settings()))


if __name__ == "__main__":
    sys.exit(main())
