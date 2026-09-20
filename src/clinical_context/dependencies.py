# ORIGIN: AI — dependency wiring typed by Claude Code, reviewed by Kiel.
"""Shared FastAPI dependencies.

Routes declare what they need with these; tests swap them out through
`app.dependency_overrides`, so no route ever builds its own client.
"""

from collections.abc import Awaitable, Callable
from functools import partial
from typing import Annotated

import httpx
from fastapi import Depends, Request

from .config import Settings, get_settings
from .fhir_client import FhirClient
from .models import ClinicalContextPacket
from .summarizer import SummaryCache, SummaryResult, summarize


def get_http(request: Request) -> httpx.AsyncClient:
    """The one shared async client, created in the app lifespan."""
    return request.app.state.http


HttpDep = Annotated[httpx.AsyncClient, Depends(get_http)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_fhir_client(http: HttpDep, settings: SettingsDep) -> FhirClient:
    return FhirClient(http, settings)


FhirDep = Annotated[FhirClient, Depends(get_fhir_client)]

# Writes the two-sentence summary for an assembled packet. It may fail, but it never changes the
# packet's facts: the route only takes the returned summary.
Summarizer = Callable[[ClinicalContextPacket], Awaitable[SummaryResult]]


def get_summary_cache(request: Request) -> SummaryCache:
    """The process-wide memory of finished summaries, created in the app lifespan."""
    return request.app.state.summary_cache


def get_summarizer(
    http: HttpDep, settings: SettingsDep, cache: Annotated[SummaryCache, Depends(get_summary_cache)]
) -> Summarizer:
    return partial(summarize, http=http, settings=settings, cache=cache)


SummarizerDep = Annotated[Summarizer, Depends(get_summarizer)]
