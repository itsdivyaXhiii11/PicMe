"""Person B (B7) - mock PicMe server for load testing WITHOUT Supabase / Google Drive.

It serves the REAL routes_download.py + zipstream.py + drive_client.py, but with
fake sessions (Person A's code needs the AI model + Postgres) and a fake
"Google Drive" that has latency and can throw temporary rate-limit errors.

    python loadtest_server.py --port 8765 --photos 400 --latency 0.05 --error-rate 0.05

Environment (same names as the real app): ZIP_PART_SIZE, DRIVE_CONCURRENCY, ...
The load-test token is printed as   TOKEN=loadtest
"""
import argparse
import os
import random
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import b_mock_backend as mock  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=8765)
ap.add_argument("--photos", type=int, default=400)
ap.add_argument("--latency", type=float, default=0.05, help="seconds per fake Drive download")
ap.add_argument("--error-rate", type=float, default=0.05, help="chance a Drive call fails with 429")
ap.add_argument("--photo-kb", type=int, default=0, help="make fake photos this big (0 = ~20KB)")
args = ap.parse_args()

mock.install()
ids = mock.make_photos(args.photos)
mock.add_session("loadtest", ids, ttl=86400)

if args.photo_kb:
    _orig = mock.photo_bytes
    mock.photo_bytes = lambda pid: (_orig(pid) * (args.photo_kb * 1024 // 20000 + 1))[:args.photo_kb * 1024]

import common  # noqa: E402
import drive_client  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402

import routes_download  # noqa: E402

common.drive = lambda: object()          # every thread gets its own fake client
rng_lock = threading.Lock()


class LoadDrive(mock.FakeDrive):
    def __call__(self, svc, pid):
        with rng_lock:
            flaky = random.random() < args.error_rate
        if flaky:
            self.fail_first[pid] = self.fail_first.get(pid, 0) + 1   # one temporary 429
        return super().__call__(svc, pid)


fake = LoadDrive(latency=args.latency)
drive_client._raw_fetch = fake

app = FastAPI()
app.include_router(routes_download.router)


@app.get("/__stats")
def stats():
    return {"drive": drive_client.STATS, "fake_max_in_flight": fake.max_in_flight,
            "fake_calls": fake.calls, "limit": drive_client.DRIVE_CONCURRENCY,
            "pid": os.getpid(), "expected_ids": len(ids)}


@app.get("/__expected")
def expected():
    import hashlib
    sess = mock.get_session("loadtest")
    return {"files": {f"{i + 1:03d}_{mock.NAMES[p]}": hashlib.sha256(mock.photo_bytes(p)).hexdigest()
                      for i, p in enumerate(sess)}}


print(f"TOKEN=loadtest  photos={args.photos} part_size={routes_download.ZIP_PART_SIZE} "
      f"drive_concurrency={drive_client.DRIVE_CONCURRENCY}", flush=True)
uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
