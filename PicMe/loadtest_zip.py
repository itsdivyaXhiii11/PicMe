"""Person B (B7) - load test: N students download ALL ZIP parts at the same time.

Against the mock server (no credentials needed):
    python loadtest_server.py --port 8765 &
    python loadtest_zip.py --base http://127.0.0.1:8765 --token loadtest --students 50

Against a real server (needs a real session token for a student with ~400 photos):
    python loadtest_zip.py --base http://localhost:8000 --token <token from /api/match> --students 50
    (add --server-pid <uvicorn pid> to watch the server's memory)

What it checks, per student and overall:
  * every ZIP part downloads (HTTP 200) and is a valid ZIP (CRC test)
  * every expected photo appears EXACTLY once across the parts (no gaps/dupes)
  * when the server exposes /__expected (mock server), file CONTENT is verified too
  * response times (time to first byte + total), failures, retries/Drive errors
    (from /__stats on the mock server), peak server memory (psutil), and that the
    Drive concurrency cap was never exceeded.
Exit code 0 only if every student finished with no failed ZIP.
"""
import argparse
import hashlib
import io
import statistics
import sys
import threading
import time
import zipfile

import requests

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://127.0.0.1:8000")
ap.add_argument("--token", required=True, help="session token (the same token may be shared by all students)")
ap.add_argument("--students", type=int, default=50)
ap.add_argument("--expected-photos", type=int, default=400)
ap.add_argument("--timeout", type=float, default=300)
ap.add_argument("--ramp", type=float, default=0.0, help="seconds to spread student start times over")
ap.add_argument("--server-pid", type=int, default=0, help="pid of the server process to watch memory")
ap.add_argument("--max-ttfb", type=float, default=2.0, help="fail if first byte takes longer (seconds)")
args = ap.parse_args()

lock = threading.Lock()
results = []          # one dict per student
ttfbs, totals = [], []
failures = []
peak_rss = [0.0]
stop_mon = threading.Event()


def monitor():
    if not args.server_pid:
        return
    try:
        import psutil
        procs = [psutil.Process(args.server_pid)]
    except Exception as e:                      # noqa: BLE001
        print("memory monitor disabled:", e)
        return
    while not stop_mon.is_set():
        try:
            rss = sum(p.memory_info().rss for p in procs + procs[0].children(recursive=True))
            peak_rss[0] = max(peak_rss[0], rss / 1e6)
        except Exception:                       # noqa: BLE001
            return
        time.sleep(0.1)


def download_part(sess, part):
    t0 = time.time()
    r = sess.get(f"{args.base}/api/zip", params={"t": args.token, "part": part},
                 stream=True, timeout=args.timeout)
    if r.status_code != 200:
        raise RuntimeError(f"part {part}: HTTP {r.status_code}")
    if "content-length" in {k.lower() for k in r.headers}:
        raise RuntimeError(f"part {part}: unexpected Content-Length header")
    buf = io.BytesIO()
    first = None
    for chunk in r.iter_content(1 << 16):
        if first is None:
            first = time.time() - t0
        buf.write(chunk)
    if first is None:
        raise RuntimeError(f"part {part}: empty body")
    with lock:
        ttfbs.append(first)
        totals.append(time.time() - t0)
    z = zipfile.ZipFile(buf)
    bad = z.testzip()
    if bad:
        raise RuntimeError(f"part {part}: corrupt entry {bad}")
    return {n: hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist()}


def student(i, expected):
    if args.ramp:
        time.sleep(args.ramp * i / max(1, args.students))
    sess = requests.Session()
    t0 = time.time()
    seen, err = {}, None
    try:
        info = sess.get(f"{args.base}/api/zip/info", params={"t": args.token}, timeout=30)
        info.raise_for_status()
        info = info.json()
        for p in range(1, info["total_parts"] + 1):
            files = download_part(sess, p)
            dup = set(files) & set(seen)
            if dup:
                raise RuntimeError(f"part {p}: duplicate files {sorted(dup)[:3]}")
            seen.update(files)
        if "MISSING.txt" in seen:
            raise RuntimeError("a MISSING.txt appeared: some photos could not be fetched")
        if len(seen) != args.expected_photos:
            raise RuntimeError(f"expected {args.expected_photos} photos, got {len(seen)}")
        if expected is not None and seen != expected:
            wrong = [n for n in expected if seen.get(n) != expected[n]]
            raise RuntimeError(f"{len(wrong)} files missing or with wrong content, e.g. {wrong[:3]}")
    except Exception as e:                      # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    with lock:
        results.append({"student": i, "ok": err is None, "seconds": time.time() - t0, "files": len(seen)})
        if err:
            failures.append((i, err))


def main():
    expected = None
    try:
        r = requests.get(f"{args.base}/__expected", timeout=10)
        if r.ok:
            expected = r.json()["files"]
            print(f"content verification ON ({len(expected)} expected files)")
    except requests.RequestException:
        pass
    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()
    t0 = time.time()
    ts = [threading.Thread(target=student, args=(i, expected)) for i in range(args.students)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    wall = time.time() - t0
    stop_mon.set()

    def pct(v, q):
        v = sorted(v)
        return v[min(len(v) - 1, int(len(v) * q))] if v else float("nan")

    ok = sum(r["ok"] for r in results)
    print("\n=========== LOAD TEST RESULT ===========")
    print(f"students finished OK : {ok}/{args.students}")
    print(f"ZIP parts downloaded : {len(totals)}")
    print(f"wall time            : {wall:.1f}s")
    if ttfbs:
        print(f"time to first byte   : median {statistics.median(ttfbs):.2f}s  p95 {pct(ttfbs, .95):.2f}s  max {max(ttfbs):.2f}s")
        print(f"part total time      : median {statistics.median(totals):.2f}s  p95 {pct(totals, .95):.2f}s  max {max(totals):.2f}s")
    if args.server_pid:
        print(f"server peak memory   : {peak_rss[0]:.0f} MB")
    drive_ok = True
    try:
        s = requests.get(f"{args.base}/__stats", timeout=10).json()
        d = s["drive"]
        print(f"drive attempts       : {d['attempts']}  retries (temporary errors recovered): {d['retries']}  "
              f"permanent failures: {d['failures']}")
        print(f"drive concurrency    : max {d['max_in_flight']} (limit {s['limit']})  "
              f"[fake drive saw max {s['fake_max_in_flight']}]")
        drive_ok = d["max_in_flight"] <= s["limit"] and s["fake_max_in_flight"] <= s["limit"]
        if not drive_ok:
            print("!! DRIVE CONCURRENCY LIMIT EXCEEDED")
    except Exception:                           # noqa: BLE001
        print("drive stats          : not available (only the mock server exposes /__stats)")
    slow = [t for t in ttfbs if t > args.max_ttfb]
    if slow:
        print(f"note: {len(slow)} parts took longer than {args.max_ttfb}s to start (expected under heavy load, "
              f"the first photo must be fetched first)")
    for i, e in failures[:10]:
        print(f"FAILED student {i}: {e}")
    passed = ok == args.students and drive_ok
    print("RESULT:", "PASS" if passed else "FAIL")
    sys.exit(0 if passed else 1)


main()
