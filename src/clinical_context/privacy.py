# ORIGIN: H-spec — Kiel's decision: no patient identifier is ever printed to a log. Not the id, not
#   an id inside a request path, and not a hash of the id that could be guessed back. Lines typed
#   by Claude Code.
"""Logging must never carry patient data: no names, facts, or ids."""

import hashlib
import hmac
import logging
import re
import secrets
import traceback

# The key exists only in this process's memory. A plain sha256 of an id can be reversed by hashing
# every likely id (a HAPI id like 1000, an SSN typed by mistake); a keyed hash cannot. The price is
# that a hash means nothing after a restart, which is fine: it only ties together the log lines of
# one request, and a real audit trail of who looked at whom belongs in a separate, protected store.
_KEY = secrets.token_bytes(32)


def patient_hash(patient_id: str) -> str:
    """A short tag that ties one request's log lines together and identifies no one."""
    return hmac.new(_KEY, patient_id.encode(), hashlib.sha256).hexdigest()[:8]


_PATIENT_IN_PATH = re.compile(r"(/v1/patients/)[^/?\s\"']+")


def _redact(text: str) -> str:
    return _PATIENT_IN_PATH.sub(r"\1{id}", text)


class RedactPatientIds(logging.Filter):
    """Rewrites `/v1/patients/<anything>/...` to `/v1/patients/{id}/...` before a record is printed.

    Attached to uvicorn's access logger, whose lines carry the full request path, and that path
    carries the patient id.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact(a) if isinstance(a, str) else a for a in record.args)
        return True


def error_location(error: BaseException) -> str:
    """`file.py:123`, where an error was raised. Never its message, which can hold patient data."""
    last = traceback.extract_tb(error.__traceback__)[-1]
    return f"{last.filename.rsplit('/', 1)[-1]}:{last.lineno}"
