import threading
from dotenv import load_dotenv

load_dotenv(override=True)

import numpy as np
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from insightface.app import FaceAnalysis

from common import drive
from db import list_faces, list_photos

#switch to s(small)
fa = FaceAnalysis(name="buffalo_s", providers=["CPUExecutionProvider"])
fa.prepare(ctx_id=-1, det_size=(320, 320))

# ----------------------------------------------------------------- Drive
# One Drive client for the whole server process.
# drive() returns None when no service_account.json exists -> local ./photos.
# (Person B may later replace this with one client per thread.)
svc = drive()
svc_lock = threading.Lock()  # the Drive client isn't thread-safe

# ----------------------------------------------------------------- index
def load_index():
    """Read every face and every photo name from Postgres and build
    the numpy matrix that matching multiplies against (MAT @ embedding)."""
    photos = list_photos()
    names = {p[0]: p[1] for p in photos}
    rows = list_faces()
    ids = [r[0] for r in rows]
    mat = (np.vstack([np.asarray(r[1], dtype=np.float32) for r in rows])
           if rows else np.zeros((0, 512), np.float32))
    return ids, mat, names


IDS, MAT, NAMES = load_index()
print(f"Loaded {len(NAMES)} photos, {len(IDS)} faces")

# ------------------------------------------------------------------- app
app = FastAPI(title="PicMe")

# The routers are imported LAST on purpose: each one does
# `from main import IDS, MAT, NAMES, fa` - those names only exist because
# everything above this line has already run.
from routes_match import router as match_router           # noqa: E402
from routes_download import router as download_router     # noqa: E402
from routes_admin import router as admin_router           # noqa: E402
from routes_match import MAX_TRIES, PROXY_HOPS, THRESHOLD, WINDOW   # noqa: E402

print(f"Config: threshold={THRESHOLD}  max_tries={MAX_TRIES}  window={WINDOW}s  "
      f"proxy_hops={PROXY_HOPS}")

app.include_router(match_router)
app.include_router(download_router)
app.include_router(admin_router)

# The website itself (static/index.html). Keep this line LAST so it only
# catches URLs that no API route claimed.
from routes_import import router as import_router
app.include_router(import_router)

app.mount("/", StaticFiles(directory="static", html=True), name="static")
