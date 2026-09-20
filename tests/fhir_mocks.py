# ORIGIN: AI — FHIR response builders typed by Claude Code, reviewed by Kiel.
"""Shapes of the FHIR responses the fake HAPI returns."""

BASE = "http://hapi.test/fhir"


def entry(resource: dict, mode: str = "match") -> dict:
    return {"resource": resource, "search": {"mode": mode}}


def bundle(
    resources: list[dict], *, next_url: str | None = None, entries: list[dict] | None = None
):
    """A searchset Bundle of matching resources (or raw `entries` for unusual cases)."""
    body: dict = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": entries if entries is not None else [entry(r) for r in resources],
    }
    if not body["entry"]:
        del body["entry"]  # HAPI omits `entry` entirely for an empty result
    if next_url:
        body["link"] = [{"relation": "self", "url": BASE}, {"relation": "next", "url": next_url}]
    return body


def operation_outcome(text: str = "Resource not found") -> dict:
    return {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "not-found", "diagnostics": text}],
    }
