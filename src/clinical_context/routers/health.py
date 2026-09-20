# ORIGIN: AI — route and probe code typed by Claude Code, reviewed by Kiel.
#   The /health contract and the edge cases are labeled separately below.
"""GET /health: liveness plus a separate status for each backend."""

import asyncio
from typing import Literal

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

from ..config import Settings
from ..dependencies import HttpDep, SettingsDep

router = APIRouter(tags=["health"])

OLLAMA_PROBE_TIMEOUT_SECONDS = 5.0


# ORIGIN: H-spec — the /health contract is Kiel's decision: each backend reported
#   separately; "missing" (Ollama up, model not pulled) is distinct from "down"; always HTTP 200
#   while the process is up. Lines typed by Claude Code.
class HealthResponse(BaseModel):
    hapi: Literal["ok", "down"]
    ollama_http: Literal["ok", "down"]
    ollama_model: Literal["ok", "missing", "down"]


# ORIGIN: H-spec — Kiel's decision: any non-200 metadata response counts as "down".
#   Lines typed by Claude Code.
async def _probe_hapi(http: httpx.AsyncClient, settings: Settings) -> Literal["ok", "down"]:
    try:
        response = await http.get(
            f"{settings.fhir_base_url}/metadata", timeout=settings.fhir_timeout_seconds
        )
    except httpx.HTTPError:
        return "down"
    return "ok" if response.status_code == 200 else "down"


# ORIGIN: H-spec — Kiel's decision: an untagged model name matches ":latest" (Ollama's own
#   rule). Lines typed by Claude Code.
def _model_present(configured: str, available: set[str]) -> bool:
    wanted = configured if ":" in configured else f"{configured}:latest"
    return wanted in available


# ORIGIN: H-spec — Kiel's decision: a non-2xx status or an unparseable /api/tags body counts
#   as "down". Lines typed by Claude Code.
async def _probe_ollama(
    http: httpx.AsyncClient, settings: Settings
) -> tuple[Literal["ok", "down"], Literal["ok", "missing", "down"]]:
    try:
        response = await http.get(
            f"{settings.ollama_host}/api/tags", timeout=OLLAMA_PROBE_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        models = response.json().get("models", [])
    except (httpx.HTTPError, ValueError):
        return "down", "down"
    available = {m.get("name") or m.get("model") for m in models}
    return "ok", "ok" if _model_present(settings.ollama_model, available) else "missing"


@router.get("/health", response_model=HealthResponse)
async def health(http: HttpDep, settings: SettingsDep) -> HealthResponse:
    """Liveness plus dependency visibility. Always 200 while the process is up."""
    hapi, (ollama_http, ollama_model) = await asyncio.gather(
        _probe_hapi(http, settings), _probe_ollama(http, settings)
    )
    return HealthResponse(hapi=hapi, ollama_http=ollama_http, ollama_model=ollama_model)
