# ORIGIN: AI — dependency wiring typed by Claude Code, reviewed by Kiel.
"""Shared FastAPI dependencies.

Routes declare what they need with these; tests swap them out through
`app.dependency_overrides`, so no route ever builds its own client.
"""

from typing import Annotated

import httpx
from fastapi import Depends, Request

from .config import Settings, get_settings


def get_http(request: Request) -> httpx.AsyncClient:
    """The one shared async client, created in the app lifespan."""
    return request.app.state.http


HttpDep = Annotated[httpx.AsyncClient, Depends(get_http)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
