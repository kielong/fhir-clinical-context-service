# ORIGIN: AI — app wiring and lifespan typed by Claude Code, reviewed by Kiel.
#   Kiel's decision: main.py only wires things together (lifespan, routers); routes live in
#   routers/ and shared dependencies in dependencies.py, the conventional FastAPI layout.
"""FastAPI app entry point: `uvicorn clinical_context.main:app`."""

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from .routers import health


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One shared async client for the life of the process: nothing blocks the event loop.
    async with httpx.AsyncClient() as client:
        app.state.http = client
        yield


app = FastAPI(title="Clinical Context Packet Service", lifespan=lifespan)
app.include_router(health.router)
