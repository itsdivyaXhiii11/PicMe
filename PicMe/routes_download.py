"""Person B - download routes (B2, B4).

  GET /api/thumb/{pid}?t=TOKEN        -> small preview for the grid
  GET /api/photo/{pid}?t=TOKEN        -> the full photo as an attachment
  GET /api/zip/info?t=TOKEN           -> how many ZIP parts there are (for the buttons)
  GET /api/zip?t=TOKEN&part=N         -> ZIP part N (default 1), streamed

Every route checks the session through Person A's functions:
  get_session(token)  -> photo ids (401 if missing/expired), already sorted
                         by (name, id) - we KEEP that order, never re-sort.
  allowed(token, pid) -> same + 403 if the photo is not in this session.
"""
import io
import mimetypes
import os
import threading
import time
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse

import db
import drive_client
import storage
import zipstream
from common import THUMBS
from routes_match import allowed, get_photo, get_session

router = APIRouter()


def _int_env(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


ZIP_PART_SIZE = _int_env("ZIP_PART_SIZE", 50)
THUMB_SIZE = 600
THUMB_QUALITY = 80


# ------------------------------------------------------------------ helpers
def _photo_name(pid):
    try:
        return get_photo(pid)
    except HTTPException:
        return "photo.jpg"


def content_disposition(filename, kind="attachment"):
    """A Content-Disposition header that cannot be broken by odd file names.

    * the plain filename="..." only keeps safe ASCII characters (no quotes,
      no slashes, no line breaks -> no header injection)
    * filename*=UTF-8''... carries the real name, percent-encoded."""
    base = os.path.basename(str(filename).replace("\\", "/")) or "download"
    ascii_name = "".join(c if (c.isascii() and (c.isalnum() or c in "._- ")) else "_"
                         for c in base) or "download"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(base, safe='')}"


def total_parts(n_photos, size=None):
    size = size or ZIP_PART_SIZE
    return (n_photos + size - 1) // size


def part_slice(ids, part, size=None):
    """The photo ids of ZIP part `part` (1-based). `ids` keeps A's order."""
    size = size or ZIP_PART_SIZE
    return ids[(part - 1) * size: part * size]


# ---------------------------------------------------------------- thumbnails
_thumb_locks = [threading.Lock() for _ in range(64)]   # one build at a time per photo
_paths_cache = {"at": 0.0, "map": {}}
_paths_lock = threading.Lock()


def thumb_file(pid):
    """Same name Person A's ingest.py uses: data/thumbs/<id>.jpg"""
    return f"{THUMBS}/{pid.replace('/', '_').replace(':', '_')}.jpg"


def _storage_path(pid):
    """Supabase Storage path of this photo's thumbnail (cached for a minute)."""
    with _paths_lock:
        fresh = time.time() - _paths_cache["at"] < 60
        if fresh and pid in _paths_cache["map"]:
            return _paths_cache["map"][pid]
    try:
        mapping = db.list_photo_ids()
    except Exception:                       # noqa: BLE001 - DB hiccup: fall back to the original
        return None
    with _paths_lock:
        _paths_cache.update(at=time.time(), map=mapping)
    return mapping.get(pid)


def _save_local(path, data):
    """Best-effort cache write (atomic). Failure is fine - we still serve the bytes."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{threading.get_ident()}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except OSError:
        pass


def _make_thumb(original):
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(original))).convert("RGB")
    img.thumbnail((THUMB_SIZE, THUMB_SIZE))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=THUMB_QUALITY)
    return buf.getvalue()


def thumb_bytes(pid):
    """Recover a missing thumbnail: Supabase Storage first, else rebuild from
    the original. Returns bytes or None."""
    path = _storage_path(pid)
    if path:
        try:
            data = storage.download(path)
            if data:
                return data
        except Exception:                   # noqa: BLE001
            pass
    try:
        return _make_thumb(drive_client.fetch_original(pid))
    except Exception:                       # noqa: BLE001
        return None


# -------------------------------------------------------------------- routes
@router.get("/api/thumb/{pid:path}")
def thumb(pid: str, t: str = ""):
    allowed(t, pid)                         # 401 expired / 403 not your photo
    headers = {"Cache-Control": "private, max-age=3600"}
    path = thumb_file(pid)
    if os.path.isfile(path):
        return FileResponse(path, media_type="image/jpeg", headers=headers)
    with _thumb_locks[hash(pid) % len(_thumb_locks)]:
        if os.path.isfile(path):            # another request just rebuilt it
            return FileResponse(path, media_type="image/jpeg", headers=headers)
        data = thumb_bytes(pid)
    if data is None:
        raise HTTPException(404, "Preview not available")
    _save_local(path, data)
    return Response(data, media_type="image/jpeg", headers=headers)


@router.get("/api/photo/{pid:path}")
def photo(pid: str, t: str = ""):
    allowed(t, pid)
    name = get_photo(pid)
    try:
        data = drive_client.fetch_original(pid)
    except drive_client.FetchError:
        raise HTTPException(502, "Could not fetch this photo right now. Please try again.")
    return Response(data, media_type=mimetypes.guess_type(name)[0] or "image/jpeg",
                    headers={"Content-Disposition": content_disposition(name),
                             "X-Content-Type-Options": "nosniff"})


@router.get("/api/zip/info")
def zip_info(t: str = ""):
    """Tells the page how many ZIP buttons to draw."""
    ids = get_session(t)
    n = len(ids)
    parts = []
    for p in range(1, total_parts(n) + 1):
        first = (p - 1) * ZIP_PART_SIZE + 1
        last = min(p * ZIP_PART_SIZE, n)
        parts.append({"part": p, "first": first, "last": last, "count": last - first + 1})
    return {"total_photos": n, "part_size": ZIP_PART_SIZE, "total_parts": len(parts),
            "parts": parts}


@router.get("/api/zip")
def zip_part(t: str = "", part: int = 1):
    ids = get_session(t)                    # 401 if expired; A's (name, id) order
    parts = total_parts(len(ids))
    if parts == 0:
        raise HTTPException(404, "There are no photos in this session")
    if part < 1 or part > parts:
        raise HTTPException(400, f"Part must be between 1 and {parts}")
    chosen = part_slice(ids, part)
    start = (part - 1) * ZIP_PART_SIZE + 1
    items = [(pid, _photo_name(pid)) for pid in chosen]
    filename = ("my-graduation-photos.zip" if parts == 1
                else f"my-graduation-photos-part{part}-of-{parts}.zip")
    # No Content-Length on purpose: the size is unknown while we stream.
    return StreamingResponse(
        zipstream.async_zip_stream(items, start=start),
        media_type="application/zip",
        headers={"Content-Disposition": content_disposition(filename),
                 "X-Zip-Part": str(part), "X-Zip-Parts": str(parts),
                 "Cache-Control": "no-store"})
