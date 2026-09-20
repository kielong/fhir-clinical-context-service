# ORIGIN: H-spec — Kiel's decision: the same packet must give the same words. Ollama's prompt
#   cache changes the arithmetic, so even at temperature 0 with a fixed seed the same prompt can
#   come back worded differently depending on what ran before it (measured: three different
#   answers for one patient across cold, warm and partly warm states). Decoding cannot be pinned
#   past that, so the finished summary is pinned instead: remembered under a hash of exactly what
#   produced it and returned verbatim next time. Only finished, checked summaries are remembered,
#   never failures. The gate lets one model call run at a time. Lines typed by Claude Code. The
#   cache lives in this process's memory only (it holds summary text, which is patient data): it
#   is empty after a restart, so a restart can re-word a summary once.
"""A small in-memory memory of finished summaries, the gate that serializes model calls, and the
background jobs that fill it.

A job belongs to the cache, not to the request that started it. A request that stops waiting
(a deadline, a closed browser tab) leaves the job running, so a model call already paid for still
ends up remembered for the next request. Requests for the same key share one job.
"""

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from typing import Any

from ..models import SummaryBlock
from ..privacy import error_location

logger = logging.getLogger("clinical_context.llm.cache")


class SummaryCache:
    def __init__(self, maxsize: int) -> None:
        self._maxsize = maxsize
        self._entries: OrderedDict[str, SummaryBlock] = OrderedDict()
        self._jobs: dict[str, asyncio.Task[Any]] = {}
        self.gate = asyncio.Lock()  # one model call at a time

    @property
    def remembers(self) -> bool:
        """False when the size is 0: nothing can be remembered, so no job is worth keeping."""
        return self._maxsize > 0

    def get(self, key: str) -> SummaryBlock | None:
        block = self._entries.get(key)
        if block is not None:
            self._entries.move_to_end(key)
        return block

    def put(self, key: str, block: SummaryBlock) -> None:
        if not self.remembers:
            return
        self._entries[key] = block
        self._entries.move_to_end(key)
        while len(self._entries) > self._maxsize:
            self._entries.popitem(last=False)  # forget the least recently used

    def job[T](self, key: str, make: Callable[[], Coroutine[Any, Any, T]]) -> asyncio.Task[T]:
        """The job running for this key, or a new one started from `make()`."""
        task = self._jobs.get(key)
        if task is None:
            task = asyncio.create_task(make())
            self._jobs[key] = task
            task.add_done_callback(lambda done: self._forget(key, done))
        return task

    def _forget(self, key: str, task: asyncio.Task[Any]) -> None:
        if self._jobs.get(key) is task:
            del self._jobs[key]
        # Only the type and place of a crash are logged: its message can hold patient data. Reading
        # the exception here also means one nobody was waiting for is never reported as forgotten.
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error(
                "summary job crashed: %s at %s", type(error).__name__, error_location(error)
            )

    async def close(self) -> None:
        """Stop every running job (at shutdown), and wait until they are really stopped."""
        jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
