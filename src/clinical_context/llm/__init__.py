# ORIGIN: AI — package surface typed by Claude Code, reviewed by Kiel.
"""Everything that touches the language model, kept apart from the deterministic pipeline.

  prompt.py      what the model is told (a pure function of the packet)
  ollama.py      the structured chat request, and the warm-up that loads the model
  checks.py      what the model may say; anything that fails is thrown away
  cache.py       remembered summaries, the one-call-at-a-time gate, background jobs
  summarizer.py  ties them together: summarize(packet) -> SummaryResult

The rest of the service uses only the four names exported here.
"""

from .cache import SummaryCache
from .ollama import warm_up
from .summarizer import SummaryResult, summarize

__all__ = ["SummaryCache", "SummaryResult", "summarize", "warm_up"]
