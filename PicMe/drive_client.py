"""Person B (B3) - fetching original photos safely under load.

What this file does, in plain words:
  * The Google Drive client is NOT thread-safe. The old code used one global
    lock, so only one download could run at a time. Here every worker thread
    gets its OWN client (threading.local), so no global lock is needed.
  * A semaphore caps how many downloads run at the same moment
    (DRIVE_CONCURRENCY, default 5) so Google does not throttle us.
  * Temporary errors (rate limits, 5xx, network hiccups) are retried with
    exponential backoff + a little random jitter. Permanent errors
    (file not found, no permission) fail straight away.
  * Local photos (ids like "local:p1_einstein.jpg") keep working: we simply
    call common.fetch_bytes(), which already handles them.

Main function:  fetch_original(pid) -> bytes   (raises FetchError on failure)

Settings (.env):
  DRIVE_CONCURRENCY=5     simultaneous downloads
  DRIVE_MAX_RETRIES=5     retries after the first attempt
  DRIVE_BACKOFF_BASE=0.5  first wait in seconds (doubles each time)
  DRIVE_BACKOFF_MAX=16    longest single wait in seconds
"""
import os
import random
import socket
import threading
import time

import common

# --------------------------------------------------------------- settings
def _int_env(name, default, minimum=1):
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except ValueError:
        return default


def _float_env(name, default):
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


DRIVE_CONCURRENCY = _int_env("DRIVE_CONCURRENCY", 5)
MAX_RETRIES = _int_env("DRIVE_MAX_RETRIES", 5, minimum=0)
BACKOFF_BASE = _float_env("DRIVE_BACKOFF_BASE", 0.5)
BACKOFF_MAX = _float_env("DRIVE_BACKOFF_MAX", 16.0)

_slots = threading.BoundedSemaphore(DRIVE_CONCURRENCY)
_local = threading.local()          # one Drive client per thread lives here
_stats_lock = threading.Lock()
STATS = {"attempts": 0, "retries": 0, "failures": 0, "successes": 0,
         "in_flight": 0, "max_in_flight": 0}

# Test hooks (kept tiny on purpose): tests replace these.
_sleep = time.sleep
_raw_fetch = None   # callable(svc, pid) -> bytes ; None = common.fetch_bytes


class FetchError(Exception):
    """A photo could not be fetched (after retries). The message is safe to
    log but is never shown to students."""


# ------------------------------------------------------------ per-thread client
def _client():
    """This thread's own Drive client (None = no service account -> local only)."""
    if not hasattr(_local, "svc"):
        _local.svc = common.drive()
    return _local.svc


def _drop_client():
    """Forget this thread's client so the next attempt builds a fresh one."""
    if hasattr(_local, "svc"):
        del _local.svc


# ---------------------------------------------------------------- classify
RETRY_STATUS = {408, 429, 500, 502, 503, 504}
RETRY_403_REASONS = {"ratelimitexceeded", "userratelimitexceeded", "dailylimitexceeded",
                     "backenderror", "sharingratelimitexceeded"}


def _http_status_and_reason(exc):
    """Pull (status, reason) out of a googleapiclient HttpError, if it is one."""
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    reason = ""
    details = getattr(exc, "error_details", None)
    if isinstance(details, list) and details and isinstance(details[0], dict):
        reason = str(details[0].get("reason", ""))
    if not reason:
        text = ""
        content = getattr(exc, "content", b"")
        if isinstance(content, bytes):
            text = content.decode("utf-8", "ignore").lower()
        for r in RETRY_403_REASONS:
            if r in text:
                reason = r
                break
    return (int(status) if status is not None else None), reason.lower()


def is_retryable(exc):
    """True for temporary problems (worth waiting and trying again)."""
    status, reason = _http_status_and_reason(exc)
    if status is not None:
        if status in RETRY_STATUS:
            return True
        if status == 403 and reason in RETRY_403_REASONS:
            return True
        return False                       # 404 / 401 / other 403 = permanent
    if isinstance(exc, FileNotFoundError | PermissionError | IsADirectoryError):
        return False                       # missing local photo: retrying won't help
    if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout, socket.gaierror)):
        return True
    if isinstance(exc, OSError):           # ssl errors, broken pipes, ...
        return True
    return False


def backoff_delay(attempt):
    """Wait before retry number `attempt` (1, 2, 3 ...): 0.5, 1, 2, 4 ... capped."""
    delay = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** (attempt - 1)))
    return delay * (0.5 + random.random() / 2)      # jitter: 50%-100% of delay


# --------------------------------------------------------------- the fetch
def _one_attempt(pid):
    with _slots:                                   # <= DRIVE_CONCURRENCY at once
        with _stats_lock:
            STATS["attempts"] += 1
            STATS["in_flight"] += 1
            STATS["max_in_flight"] = max(STATS["max_in_flight"], STATS["in_flight"])
        try:
            fn = _raw_fetch or common.fetch_bytes
            svc = None if pid.startswith("local:") else _client()
            if svc is None and not pid.startswith("local:"):
                raise FetchError("Google Drive is not configured")
            return fn(svc, pid)
        finally:
            with _stats_lock:
                STATS["in_flight"] -= 1


def fetch_original(pid):
    """Download one original photo -> bytes.

    Retries temporary errors with exponential backoff. The semaphore is
    released while we wait, so a sleeping retry never blocks other downloads."""
    attempt = 0
    while True:
        try:
            data = _one_attempt(pid)
            with _stats_lock:
                STATS["successes"] += 1
            return data
        except FetchError:
            with _stats_lock:
                STATS["failures"] += 1
            raise
        except Exception as exc:                    # noqa: BLE001
            if not is_retryable(exc):
                with _stats_lock:
                    STATS["failures"] += 1
                raise FetchError(f"permanent error for {pid}: {type(exc).__name__}") from exc
            _drop_client()                          # connection may be poisoned
            attempt += 1
            if attempt > MAX_RETRIES:
                with _stats_lock:
                    STATS["failures"] += 1
                raise FetchError(f"gave up on {pid} after {MAX_RETRIES} retries: "
                                 f"{type(exc).__name__}") from exc
            with _stats_lock:
                STATS["retries"] += 1
            _sleep(backoff_delay(attempt))


def reset_stats():
    with _stats_lock:
        for k in STATS:
            STATS[k] = 0
