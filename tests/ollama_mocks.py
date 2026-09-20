# ORIGIN: AI — fake Ollama responses typed by Claude Code, reviewed by Kiel.
"""What Ollama's /api/chat returns, so tests can script a model without running one."""

import json

OLLAMA = "http://ollama.test"
CHAT = f"{OLLAMA}/api/chat"

GOOD = (
    "The patient has recorded active prediabetes and anemia. "
    "No medications or allergies are recorded."
)

# A reply that claims nothing about what is absent, so it is true for any patient.
NEUTRAL = "The patient has a chart with recorded entries. See the record for the details."


def reply(summary: str | None = GOOD, *, content: str | None = None, **durations_ns) -> dict:
    """A finished /api/chat response. Durations are in nanoseconds, as Ollama reports them."""
    body = content if content is not None else json.dumps({"summary": summary})
    return {
        "model": "llama3.2:3b",
        "message": {"role": "assistant", "content": body},
        "done": True,
        "done_reason": "stop",
        "total_duration": durations_ns.get("total_duration", 4_000_000_000),
        "load_duration": durations_ns.get("load_duration", 500_000_000),
        "prompt_eval_count": durations_ns.get("prompt_eval_count", 210),
        "prompt_eval_duration": durations_ns.get("prompt_eval_duration", 900_000_000),
        "eval_count": durations_ns.get("eval_count", 48),
        "eval_duration": durations_ns.get("eval_duration", 2_400_000_000),
    }
