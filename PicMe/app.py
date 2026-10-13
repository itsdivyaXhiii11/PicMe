import mimetypes, re, secrets, tempfile, threading, time, zipfile
from collections import defaultdict

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from insightface.app import FaceAnalysis

from common import ALLOW_UPLOAD, THUMBS, db, drive, fetch_bytes

THRESHOLD = 0.45        # cosine similarity; raise = stricter, lower = more matches
SESSION_SECONDS = 3600  # how long a scan stays valid
MAX_TRIES, WINDOW = 10, 600  # selfie attempts per IP per 10 min

app = FastAPI()
fa = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
fa.prepare(ctx_id=-1, det_size=(640, 640))
svc = drive()  # None when running on local ./photos only
svc_lock = threading.Lock()  # Drive client isn't thread-safe


def load_index():
    con = db()
    rows = con.execute("SELECT photo_id, emb FROM faces").fetchall()
    names = dict(con.execute("SELECT id, name FROM photos").fetchall())
    ids = [r[0] for r in rows]
    mat = (np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
           if rows else np.zeros((0, 512), np.float32))
    return ids, mat, names


IDS, MAT, NAMES = load_index()
print(f"Loaded {len(NAMES)} photos, {len(IDS)} faces")
SESSIONS: dict = {}  # token -> (expiry, set(photo_ids))
HITS = defaultdict(list)


def allowed(token, pid=None):
    s = SESSIONS.get(token)
    if not s or s[0] < time.time():
        raise HTTPException(401, "Session expired. Scan your face again.")
    if pid is not None and pid not in s[1]:
        raise HTTPException(403, "Not your photo")
    return s[1]


def thumb_file(pid):
    return f"{THUMBS}/{pid.replace('/', '_').replace(':', '_')}.jpg"


def safe_name(n):
    return re.sub(r'[^A-Za-z0-9._ -]', "_", n)


def fetch(pid):
    with svc_lock:
        return fetch_bytes(svc, pid)


@app.get("/api/status")
def status():
    return {"photos": len(NAMES), "faces": len(IDS), "allow_upload": ALLOW_UPLOAD}


@app.post("/api/match")
def match(request: Request, file: UploadFile = File(...)):
    ip, now = request.client.host, time.time()
    HITS[ip] = [t for t in HITS[ip] if now - t < WINDOW]
    if len(HITS[ip]) >= MAX_TRIES:
        raise HTTPException(429, "Too many tries. Wait a few minutes and try again.")
    HITS[ip].append(now)

    img = cv2.imdecode(np.frombuffer(file.file.read(), np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "Couldn't read that image.")
    faces = fa.get(img)
    if len(faces) != 1:
        raise HTTPException(400, "We need exactly one clear face in the frame. Try again in better light.")

    sims = MAT @ faces[0].normed_embedding
    hit = sorted({str(IDS[i]) for i in np.where(sims >= THRESHOLD)[0]}, key=lambda p: NAMES[p])

    for k in [k for k, v in SESSIONS.items() if v[0] < now]:
        del SESSIONS[k]
    token = secrets.token_urlsafe(24)
    SESSIONS[token] = (now + SESSION_SECONDS, set(hit))
    return {"token": token, "photos": [{"id": p, "name": NAMES[p]} for p in hit]}


@app.get("/api/thumb/{pid:path}")
def thumb(pid: str, t: str):
    allowed(t, pid)
    return FileResponse(thumb_file(pid))


@app.get("/api/photo/{pid:path}")
def photo(pid: str, t: str):
    allowed(t, pid)
    name = NAMES[pid]
    return Response(fetch(pid), media_type=mimetypes.guess_type(name)[0] or "image/jpeg",
                    headers={"Content-Disposition": f'attachment; filename="{safe_name(name)}"'})


@app.get("/api/zip")
def zip_all(t: str):
    ids = sorted(allowed(t), key=lambda p: NAMES[p])
    tmp = tempfile.SpooledTemporaryFile(max_size=200 * 1024 * 1024)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        for n, pid in enumerate(ids, 1):
            z.writestr(f"{n:03d}_{safe_name(NAMES[pid])}", fetch(pid))
    size = tmp.tell()
    tmp.seek(0)
    return StreamingResponse(iter(lambda: tmp.read(1 << 20), b""), media_type="application/zip",
                             headers={"Content-Length": str(size),
                                      "Content-Disposition": 'attachment; filename="my-graduation-photos.zip"'})


app.mount("/", StaticFiles(directory="static", html=True), name="static")
