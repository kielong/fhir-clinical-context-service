# ORIGIN: H-spec — the rules below are Kiel's decisions: a patient is looked up by id first and
#   then by identifier (the assignment's example id is a Synthea identifier, not a HAPI id); no
#   match is "not found" and two matches is "ambiguous"; every page of a search is fetched and the
#   client never stops at the display cap; no status filter in the query and never $everything;
#   each next link is re-based onto the configured server; searches bypass HAPI's search cache;
#   and a page limit makes a runaway search fail instead of looping. Lines typed by Claude Code.
"""Everything that talks to the FHIR server: finding a patient, and fetching whole lists.

HOW A PATIENT IS FOUND
  1. GET Patient/{id}. A 200 is the patient.
  2. If that is a 400, 404 or 410, the id may be an identifier instead (the Synthea UUID in the
     assignment's example is one), so GET Patient?identifier={id}. No identifier system is given:
     the UUID is stored under two systems on ONE patient, so a correct match is still one patient.
  3. No match -> PatientNotFound. Two or more -> AmbiguousPatient (it means duplicates from a bad
     load; showing one of them at random would be worse than saying so).
  Any other answer (5xx, 401/403, no answer, a body that is not a Patient) -> FhirUnavailable.
  "I do not know" must never be reported as "there is no such patient".

HOW A LIST IS FETCHED
  Search Condition / MedicationRequest / AllergyIntolerance with patient={id} and _count as a PAGE
  size, then follow the Bundle's `next` link until there is none. Never stop at 25: the worst real
  patient has 1,275 medication requests. No status filter: filtering happens in assembly, which is
  what lets it report how much it left out. Never $everything.

Every request sends `Cache-Control: no-cache`: HAPI reuses identical searches for 60 seconds, so
without it a patient loaded a moment ago can look empty.
"""

from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from .config import Settings

REQUEST_HEADERS = {"Accept": "application/fhir+json", "Cache-Control": "no-cache"}
NOT_FOUND_BY_ID = frozenset({400, 404, 410})


class PatientNotFound(Exception):
    """Neither a HAPI id nor an identifier matched."""


class AmbiguousPatient(Exception):
    """More than one Patient carries this identifier."""


class FhirUnavailable(Exception):
    """The FHIR server failed, timed out, or answered with something unusable."""


def _matches(bundle: dict, resource_type: str) -> list[dict]:
    """The real results in a searchset: not `include`d extras, not an OperationOutcome."""
    found = []
    for entry in bundle.get("entry") or []:
        resource = entry.get("resource") or {}
        mode = (entry.get("search") or {}).get("mode", "match")
        if mode == "match" and resource.get("resourceType") == resource_type:
            found.append(resource)
    return found


def _next_link(bundle: dict) -> str | None:
    for link in bundle.get("link") or []:
        if link.get("relation") == "next":
            return link.get("url")
    return None


class FhirClient:
    def __init__(self, http: httpx.AsyncClient, settings: Settings) -> None:
        self._http = http
        self._base = settings.fhir_base_url.rstrip("/")
        self._timeout = settings.fhir_timeout_seconds
        self._page_size = settings.fhir_page_size
        self._max_pages = settings.fhir_max_pages

    # ------------------------------------------------------------------ plumbing

    async def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        try:
            return await self._http.get(
                url, params=params, headers=REQUEST_HEADERS, timeout=self._timeout
            )
        except httpx.HTTPError as error:  # refused, timed out, protocol error
            raise FhirUnavailable(f"{type(error).__name__} calling the FHIR server") from error

    # ORIGIN: H-spec — Kiel's decision: each next link is re-based onto the configured server,
    #   because HAPI builds paging links from its own idea of its address, which may be
    #   unreachable from here (localhost inside a container). Only the link's path and query are
    #   trusted. Lines typed by Claude Code.
    def _rebase(self, url: str) -> str:
        theirs, ours = urlsplit(url), urlsplit(self._base)
        return urlunsplit((ours.scheme, ours.netloc, theirs.path, theirs.query, ""))

    # ORIGIN: H-spec — Kiel's decision: a body that is not JSON (say, a proxy's error page) is
    #   "unavailable", not a crash. Lines typed by Claude Code.
    @staticmethod
    def _json(response: httpx.Response) -> dict:
        try:
            return response.json()
        except ValueError as error:
            raise FhirUnavailable("the FHIR server returned a body that is not JSON") from error

    # ORIGIN: H-spec — Kiel's decision: a Patient with no id cannot be given a source, so it is
    #   unusable. Lines typed by Claude Code.
    @staticmethod
    def _usable_patient(resource: dict) -> dict:
        if resource.get("resourceType") != "Patient" or not resource.get("id"):
            raise FhirUnavailable("the FHIR server returned something that is not a usable Patient")
        return resource

    # ------------------------------------------------------------------ finding a patient

    async def resolve_patient(self, patient_id: str) -> dict:
        response = await self._get(f"{self._base}/Patient/{quote(patient_id, safe='')}")
        if response.status_code == 200:
            return self._usable_patient(self._json(response))
        if response.status_code not in NOT_FOUND_BY_ID:
            raise FhirUnavailable(f"HTTP {response.status_code} looking up a Patient by id")

        # ORIGIN: H-spec — Kiel's decision: _count=2 is enough to tell one match from several
        #   without downloading them all. Lines typed by Claude Code.
        response = await self._get(
            f"{self._base}/Patient", params={"identifier": patient_id, "_count": 2}
        )
        if response.status_code != 200:
            raise FhirUnavailable(f"HTTP {response.status_code} searching Patient by identifier")
        matches = _matches(self._json(response), "Patient")
        if not matches:
            raise PatientNotFound
        if len(matches) > 1:
            raise AmbiguousPatient
        return self._usable_patient(matches[0])

    # ------------------------------------------------------------------ fetching a list

    async def search_by_patient(self, resource_type: str, fhir_id: str) -> list[dict]:
        """Every resource of this type for the patient, across all pages."""
        resources: list[dict] = []
        response = await self._get(
            f"{self._base}/{resource_type}",
            params={"patient": fhir_id, "_count": self._page_size},
        )
        for page in range(1, self._max_pages + 1):
            if response.status_code != 200:
                raise FhirUnavailable(f"HTTP {response.status_code} searching {resource_type}")
            bundle = self._json(response)
            resources.extend(_matches(bundle, resource_type))
            next_url = _next_link(bundle)
            if next_url is None:
                return resources
            if page < self._max_pages:
                response = await self._get(self._rebase(next_url))
        # Still a next link after the last allowed page: fail rather than return a partial list.
        raise FhirUnavailable(
            f"{resource_type} search needs more than {self._max_pages} pages; giving up"
        )

    # ------------------------------------------------------------------ health

    # ORIGIN: H-spec — Kiel's decision: any answer other than a 200 from the metadata endpoint,
    #   or no answer at all, counts as "down". Lines typed by Claude Code.
    async def ping(self) -> bool:
        try:
            response = await self._get(f"{self._base}/metadata")
        except FhirUnavailable:
            return False
        return response.status_code == 200
