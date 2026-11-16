"""Person B (B1) - build a ZIP and send it while it is being built.

Why: the old code built the whole ZIP in a temp file first, so a student
waited a long time and the server used lots of RAM/disk for 400 photos.

How it works (simple version):
  1. Photos are fetched in the background, a few at a time ("prefetch"),
     but always written into the ZIP in the right order.
  2. Each photo is written to the ZIP and the finished bytes are handed
     straight to the network. Nothing is saved to disk. Only a handful of
     photos (ZIP_PREFETCH) are ever in memory at once.
  3. If a photo still fails after retries, it is skipped and its name is
     listed in MISSING.txt (the last file in the ZIP).
  4. ZIP64 is on, so archives > 4 GB are fine. There is no Content-Length
     because we do not know the size in advance.

Main functions:
  zip_chunks(items, start=1)        -> a normal (sync) generator of bytes
  async_zip_stream(items, start=1)  -> async generator for StreamingResponse;
                                       stops fetching if the client disconnects
"""
import os
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

from starlette.concurrency import run_in_threadpool

import drive_client

# How many photos may be fetched ahead of the one being written.
ZIP_PREFETCH = max(1, int(os.environ.get("ZIP_PREFETCH", "3")))
# Shared pool for fetching. Real Drive concurrency is capped inside drive_client.
_POOL = ThreadPoolExecutor(max_workers=max(8, drive_client.DRIVE_CONCURRENCY * 2),
                           thread_name_prefix="zipfetch")


def safe_name(name):
    """Make a file name safe to put inside a ZIP (no folders, no odd characters)."""
    name = os.path.basename(str(name).replace("\\", "/"))
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
    return name or "photo.jpg"


class _Sink:
    """A write-only 'file' that just collects what zipfile writes.
    zipfile thinks it is writing to a stream; we empty it after every step."""

    def __init__(self):
        self.parts = []

    def write(self, b):
        self.parts.append(bytes(b))
        return len(b)

    def flush(self):
        pass

    def drain(self):
        data = b"".join(self.parts)
        self.parts = []
        return data


def _zipinfo(filename):
    zi = zipfile.ZipInfo(filename, date_time=time.localtime(time.time())[:6])
    zi.compress_type = zipfile.ZIP_STORED        # JPEGs are already compressed
    zi.external_attr = 0o644 << 16
    return zi


def _write(z, sink, filename, data):
    """Write one file into the ZIP and return the bytes produced for it."""
    zi = _zipinfo(filename)
    zi.file_size = len(data)
    # force_zip64 keeps every entry valid for huge archives; unseekable output
    # makes zipfile use data descriptors, which every unzip tool understands.
    with z.open(zi, "w", force_zip64=True) as dest:
        dest.write(data)
    return sink.drain()


def zip_chunks(items, start=1, fetch=None, prefetch=None, chunk_size=1 << 20):
    """Yield the ZIP as bytes chunks.

    items: list of (photo_id, photo_name) in the order they must appear.
    start: number of the first file (so part 2 of 50-photo parts starts at 051).
    fetch: function pid -> bytes (default: drive_client.fetch_original).
    """
    fetch = fetch or drive_client.fetch_original
    window = prefetch or ZIP_PREFETCH
    sink = _Sink()
    missing = []
    futures = []
    submitted = 0

    def submit_next():
        nonlocal submitted
        if submitted < len(items):
            futures.append(_POOL.submit(fetch, items[submitted][0]))
            submitted += 1

    try:
        with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            for _ in range(min(window, len(items))):
                submit_next()
            for idx, (pid, name) in enumerate(items):
                fut = futures[idx]
                submit_next()                     # keep the pipeline full
                filename = f"{start + idx:03d}_{safe_name(name)}"
                try:
                    data = fut.result()
                except Exception:                 # noqa: BLE001 - skip, list in MISSING.txt
                    missing.append(filename)
                    futures[idx] = None
                    continue
                futures[idx] = None               # free the photo bytes after writing
                out = _write(z, sink, filename, data)
                del data
                for i in range(0, len(out), chunk_size):
                    yield out[i:i + chunk_size]
                del out
            if missing:
                text = ("These photos could not be downloaded and are not in this ZIP.\r\n"
                        "Please download them one by one from the gallery:\r\n\r\n"
                        + "\r\n".join(missing) + "\r\n")
                out = _write(z, sink, "MISSING.txt", text.encode("utf-8"))
                yield out
        tail = sink.drain()                       # central directory (written on close)
        if tail:
            yield tail
    finally:
        for f in futures:                         # client left early -> stop pending work
            if f is not None:
                f.cancel()


async def async_zip_stream(items, start=1, fetch=None):
    """Async wrapper: never blocks the event loop, and always closes the
    generator (cancelling background fetches) when the client disconnects."""
    gen = zip_chunks(items, start=start, fetch=fetch)
    sentinel = object()
    try:
        while True:
            chunk = await run_in_threadpool(next, gen, sentinel)
            if chunk is sentinel:
                break
            yield chunk
    finally:
        gen.close()
