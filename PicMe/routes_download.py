"""Person B - download routes (split out of app.py).

  GET /api/thumb/{pid}?t=TOKEN   -> small preview for the grid
  GET /api/photo/{pid}?t=TOKEN   -> the full photo as an attachment
  GET /api/zip?t=TOKEN           -> every photo of this student in one zip

NOTE (Person A, during the split): this is a WORKING version copied from
app.py so the site keeps running today. Person B will replace the innards
with zipstream.py / drive_client.py (tasks B1-B4). The session checks below
show exactly how to use A's two functions:

    get_session(token) -> list of photo ids   (raises 401 if bad/expired)
    get_photo(id)      -> photo file name
"""
import mimetypes
import re
import tempfile
import zipfile

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse

from common import THUMBS
from main import svc, svc_lock
from routes_match import get_photo, get_session

router = APIRouter()


def thumb_file(pid):
    return f"{THUMBS}/{pid.replace('/', '_').replace(':', '_')}.jpg"


def safe_name(n):
    return re.sub(r'[^A-Za-z0-9._ -]', "_", n)


def fetch(pid):
    with svc_lock:
        from common import fetch_bytes
        return fetch_bytes(svc, pid)


@router.get("/api/thumb/{pid:path}")
def thumb(pid: str, t: str):
    ids = get_session(t)              # 401 if missing or expired
    if pid not in ids:                # 403 if it's not one of YOUR photos
        raise HTTPException(403, "Not your photo")
    return FileResponse(thumb_file(pid))


@router.get("/api/photo/{pid:path}")
def photo(pid: str, t: str):
    ids = get_session(t)
    if pid not in ids:
        raise HTTPException(403, "Not your photo")
    name = get_photo(pid)
    return Response(fetch(pid), media_type=mimetypes.guess_type(name)[0] or "image/jpeg",
                    headers={"Content-Disposition": f'attachment; filename="{safe_name(name)}"'})


@router.get("/api/zip")
def zip_all(t: str):
    ids = sorted(get_session(t), key=get_photo)   # same order as the grid
    tmp = tempfile.SpooledTemporaryFile(max_size=200 * 1024 * 1024)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        for n, pid in enumerate(ids, 1):
            z.writestr(f"{n:03d}_{safe_name(get_photo(pid))}", fetch(pid))
    size = tmp.tell()
    tmp.seek(0)
    return StreamingResponse(iter(lambda: tmp.read(1 << 20), b""), media_type="application/zip",
                             headers={"Content-Length": str(size),
                                      "Content-Disposition": 'attachment; filename="my-graduation-photos.zip"'})
