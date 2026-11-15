"""Person B - a fake backend used ONLY by tests and the load test.

It replaces Person A's `routes_match` module (which needs the AI model and
Postgres) with a tiny in-memory version that has the SAME function shapes:

    get_session(token) -> list of photo ids sorted by (name, id)   (401 if bad)
    get_photo(pid)     -> file name                                (404 if unknown)
    allowed(token, pid=None)                                       (401 / 403)

It also gives a fake "Google Drive" (with optional latency and forced
temporary errors) so we can test retries and concurrency without credentials.

Call install() BEFORE importing routes_download.
"""
import hashlib
import sys
import threading
import time
import types

from fastapi import HTTPException

NAMES = {}          # pid -> file name
SESSIONS = {}       # token -> (expires_at, [pids])
LOCK = threading.Lock()


def photo_bytes(pid):
    """Deterministic fake 'JPEG' content (~20 KB) so tests can verify it."""
    seed = hashlib.sha256(pid.encode()).digest()
    return b"\xff\xd8" + seed * 640 + b"\xff\xd9"


def make_photos(n, prefix="IMG_"):
    NAMES.clear()
    ids = []
    for i in range(n):
        pid = f"drive{i:04d}"
        NAMES[pid] = f"{prefix}{i:04d}.jpg"
        ids.append(pid)
    return ids


def add_session(token, pids, ttl=3600):
    SESSIONS[token] = (time.time() + ttl, list(pids))


def get_session(token):
    s = SESSIONS.get(token or "")
    if not s or s[0] < time.time():
        raise HTTPException(401, "Session expired. Scan your face again.")
    return sorted(s[1], key=lambda p: (NAMES.get(p, p), p))


def get_photo(pid):
    if pid not in NAMES:
        raise HTTPException(404, "No such photo")
    return NAMES[pid]


def allowed(token, pid=None):
    ids = get_session(token)
    if pid is not None and pid not in ids:
        raise HTTPException(403, "Not your photo")
    return set(ids)


def install():
    """Put the fake routes_match into sys.modules."""
    mod = types.ModuleType("routes_match")
    mod.get_session, mod.get_photo, mod.allowed = get_session, get_photo, allowed
    mod.NAMES = NAMES
    sys.modules["routes_match"] = mod
    return mod


# ------------------------------------------------------------- fake Drive
class FakeDrive:
    """Stands in for common.fetch_bytes. Counts concurrency, adds latency,
    and can raise temporary errors on demand."""

    def __init__(self, latency=0.0, fail_first=None, fail_forever=None, error=None):
        self.latency = latency
        self.fail_first = dict(fail_first or {})     # pid -> how many times to fail
        self.fail_forever = set(fail_forever or ())
        self.error = error
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls = 0
        self.clients_seen = set()

    def __call__(self, svc, pid):
        with self.lock:
            self.calls += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.clients_seen.add(id(svc))
            left = self.fail_first.get(pid, 0)
            if left > 0:
                self.fail_first[pid] = left - 1
            fail = left > 0 or pid in self.fail_forever
        try:
            if self.latency:
                time.sleep(self.latency)
            if fail:
                raise (self.error or make_http_error(429, "rateLimitExceeded"))
            return photo_bytes(pid)
        finally:
            with self.lock:
                self.in_flight -= 1


def make_http_error(status, reason="rateLimitExceeded"):
    """A real googleapiclient HttpError, like the one Google would raise."""
    from googleapiclient.errors import HttpError
    import json

    class Resp(dict):
        pass

    resp = Resp(status=status)
    resp.status = status
    resp.reason = reason
    body = json.dumps({"error": {"errors": [{"reason": reason}], "code": status}}).encode()
    return HttpError(resp, body)
