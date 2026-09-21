# ORIGIN: AI — route typed by Claude Code, reviewed by Kiel. The rules the page follows (facts
#   labeled as the FHIR record, the summary labeled as model prose, no decision offered, the
#   patient id kept out of anything a server logs) are Claude Code's proposals until Kiel
#   adopts them.
"""GET /: the thin reviewer page.

One HTML file, shipped inside the package so the Docker image has it. It is not part of the API:
the one endpoint is the packet. The page only calls that endpoint and shows what comes back.
"""

import html
from functools import cache
from importlib.resources import files

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from ..dependencies import SettingsDep

router = APIRouter(include_in_schema=False)

_PLACEHOLDER = "__FHIR_BASE__"

# The page may run its own inline script and style, and talk only to the service that served it.
# It may not load anything else, embed anything, change its base address or submit a form.
_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


@cache
def _template() -> str:
    return (files("clinical_context") / "web" / "reviewer.html").read_text(encoding="utf-8")


@router.get("/", response_class=HTMLResponse)
async def reviewer_page(settings: SettingsDep) -> HTMLResponse:
    # Where a source link points is configuration, escaped so it can never break out of the page.
    base = html.escape(settings.public_fhir_base_url.rstrip("/"), quote=True)
    return HTMLResponse(_template().replace(_PLACEHOLDER, base), headers=_HEADERS)
