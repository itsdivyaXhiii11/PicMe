"""Person A - matching routes (split out of app.py).

Endpoints:
  GET  /api/status  -> how many photos/faces are indexed
  POST /api/match   -> send a selfie, get back a token + your photo list

Also provides the two functions Person B needs (routes_download.py imports
them - do not change the shape):
    get_session(token) -> list of photo ids (raises 401 if missing/expired)
    get_photo(id)      -> the photo's file name (raises 404 if unknown)

Sessions live in Postgres (A6): the server can be restarted and everyone
keeps their photos. Only the SHA-256 hash of a token is ever stored.
"""
import os
import secrets
import threading
import time
from collections import defaultdict

import cv2
import numpy as np
from fastapi import APIRouter, File, HTTPException, Request, UploadFile

import db
from common import ALLOW_UPLOAD
from main import IDS, MAT, NAMES, get_model  

router = APIRouter()

# ------------------------------------------------------------------ config
# Everything below comes from .env (see .env.example).
THRESHOLD = float(os.environ.get("THRESHOLD", "0.45"))
"""cosine similarity; raise = stricter, lower = more matches."""
SESSION_SECONDS = int(os.environ.get("SESSION_SECONDS", "3600"))
"""how long a scan stays valid"""
MAX_TRIES = int(os.environ.get("MAX_TRIES", "300"))
"""selfie attempts per IP per WINDOW seconds (a whole campus shares one IP)"""
WINDOW = int(os.environ.get("WINDOW", "600"))
"""the window in seconds"""
PROXY_HOPS = int(os.environ.get("PROXY_HOPS", "0"))
""">0 = read the real client IP from the X-Forwarded-For header"""

# --------------------------------------------------------------- in-memory
# The rate limiter stays in RAM (it is per-server, not per-student).
# Sessions do NOT live here any more - they are in Postgres.
HITS = defaultdict(list)  # ip -> [attempt timestamps]
_hits_lock = threading.Lock()


# ------------------------------------------------------------ ip helpers
def client_ip(request: Request) -> str:
    """The IP we count against.

    With PROXY_HOPS=n there are n proxies between the student and us, and
    X-Forwarded-For looks like "student, proxy1, proxy2". The student's IP
    is therefore n entries from the right. Without proxies we just use the
    socket address.
    """
    if PROXY_HOPS >= 1:
        forwarded = request.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[len(parts) - PROXY_HOPS] if len(parts) >= PROXY_HOPS else parts[0]
    return request.client.host if request.client else "unknown"


def allow_attempt(ip: str) -> bool:
    """Count one attempt for this IP. Returns False when the IP has used up
    its tries inside the window (the caller then answers 429)."""
    now = time.time()
    with _hits_lock:
        hits = [t for t in HITS.get(ip, []) if now - t < WINDOW]
        HITS[ip] = hits
        if len(hits) >= MAX_TRIES:
            return False
        hits.append(now)
        if len(HITS) > 5000:            # never let the dict grow forever
            for k in [k for k, v in HITS.items() if not v or now - v[-1] > WINDOW]:
                del HITS[k]
        return True


# ------------------------------------------------- the two functions for B
def get_session(token):
    """Return the list of photo ids this token may access.
    Raises 401 'session expired' for a missing OR expired token."""
    s = db.get_session(db.hash_token(token or ""))
    if not s or s["expires_at"] < time.time():
        raise HTTPException(401, "Session expired. Scan your face again.")
    return sorted(s["photo_ids"], key=lambda p: (NAMES.get(p, p), p))


def get_photo(pid):
    """Return the photo's file name, or 404 if that id doesn't exist."""
    name = NAMES.get(pid)
    if name is None:
        raise HTTPException(404, "No such photo")
    return name


def allowed(token, pid=None):
    """Session check used by the download routes: valid session, and
    (optionally) that the photo belongs to this session."""
    ids = get_session(token)
    if pid is not None and pid not in ids:
        raise HTTPException(403, "Not your photo")
    return set(ids)


# ----------------------------------------------------------------- routes
@router.get("/api/status")
def status():
    return {"photos": len(NAMES), "faces": len(IDS), "allow_upload": ALLOW_UPLOAD,
            "threshold": THRESHOLD}


@router.post("/api/match")
def match(request: Request, file: UploadFile = File(...)):
    # 1) rate limit: count attempts per IP inside the time window
    if not allow_attempt(client_ip(request)):
        raise HTTPException(429, "Too many tries. Wait a few minutes and try again.")

    # 2) decode the uploaded selfie
    img = cv2.imdecode(np.frombuffer(file.file.read(), np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "Couldn't read that image.")

    # 3) the AI must see EXACTLY one clear face
    faces = get_model().get(img)
    if len(faces) != 1:
        raise HTTPException(400, "We need exactly one clear face in the frame. Try again in better light.")

    # 4) THE MATH: compare this face to every stored face in one shot
    sims = MAT @ faces[0].normed_embedding
    hit = sorted({str(IDS[i]) for i in np.where(sims >= THRESHOLD)[0]},
                 key=lambda p: NAMES.get(p, p))

    # 5) mint a session in the database: sweep the dead ones, save the new one
    db.delete_expired_sessions()
    token = secrets.token_urlsafe(24)
    db.save_session(db.hash_token(token), list(hit), time.time() + SESSION_SECONDS)
    return {"token": token, "photos": [{"id": p, "name": NAMES.get(p, p)} for p in hit]}
