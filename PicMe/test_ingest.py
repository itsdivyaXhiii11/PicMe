"""A4 + A5 end-to-end test: ingest a real photo, then match a selfie against it.

    venv\\Scripts\\python.exe test_ingest.py

It uses the group photo that ships with insightface (six real faces!), so the
whole chain is tested for real:
  photo -> ingest -> Postgres + Storage -> server -> selfie -> matches

Everything it creates is removed again at the end.
"""
import os
import shutil
import subprocess
import sys
import time

import cv2
import numpy as np
import requests
from PIL import Image

import db
import storage

PORT = 8125
BASE = f"http://127.0.0.1:{PORT}"
SRC = os.path.join(os.path.dirname(__import__("insightface").__file__), "data", "images", "t1.jpg")
PHOTO_NAME = "test_group.jpg"
PHOTO_ID = "local:" + PHOTO_NAME
SELFIE = "data/test_selfie.jpg"

FAILED = []


def check(label, ok, extra=""):
    print(("  ok   " if ok else "  FAIL ") + label + ((" -> " + str(extra)) if extra else ""))
    if not ok:
        FAILED.append(label)


def make_selfie():
    """Cut one single face out of the group photo - that is our 'selfie'."""
    from insightface.app import FaceAnalysis
    fa = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
    fa.prepare(ctx_id=-1, det_size=(640, 640))
    img = cv2.imread(SRC)
    faces = fa.get(img)
    for f in sorted(faces, key=lambda f: -(f.bbox[2] - f.bbox[0])):
        x1, y1, x2, y2 = [int(v) for v in f.bbox]
        crop = img[max(y1, 0):y2, max(x1, 0):x2]
        if crop.size == 0:
            continue
        if len(fa.get(crop)) == 1:                 # exactly one face -> a valid selfie
            cv2.imwrite(SELFIE, crop)
            return True
    return False


print("== preparing the test photo ==")
os.makedirs("photos", exist_ok=True)
shutil.copyfile(SRC, f"photos/{PHOTO_NAME}")
print(f"copied {SRC} -> photos/{PHOTO_NAME}")

print("\n== A4: running ingest.py --local ==")
before = db.photo_count()
r = subprocess.run([sys.executable, "ingest.py", "--local"], capture_output=True, text=True,
                   timeout=1800)
print(r.stdout.strip()[-1200:])
if r.returncode != 0:
    print(r.stderr.strip()[-2000:])
    sys.exit("ingest.py failed")

rows = [p for p in db.list_photos() if p[0] == PHOTO_ID]
check("the photo is in Postgres", len(rows) == 1, rows)
check("one new photo was added", db.photo_count() == before + 1, db.photo_count())
check("it has a thumb path", bool(rows and rows[0][2]), rows)
if rows and rows[0][2]:
    check("the thumbnail is really in Supabase Storage",
          storage.exists(rows[0][2]), rows[0][2])
    check("the thumbnail can be downloaded",
          len(storage.download(rows[0][2]) or b"") > 1000)
faces = [f for f in db.list_faces() if f[0] == PHOTO_ID]
check("the faces were detected and stored", len(faces) >= 1, f"{len(faces)} faces")
check("the embedding has 512 numbers", bool(faces) and len(faces[0][1]) == 512)

print("\n== A4: ingest is resumable (a second run does nothing) ==")
r = subprocess.run([sys.executable, "ingest.py", "--local"], capture_output=True, text=True,
                   timeout=1800)
check("the second run indexes 0 photos", "indexed 0 photos" in r.stdout, r.stdout.strip()[-200:])
check("the photo count did not change", db.photo_count() == before + 1, db.photo_count())

print("\n== A5: the server matches a selfie against the photo ==")
check("we could cut a single-face selfie", make_selfie())
env = dict(os.environ)
log = open(os.path.join(os.environ.get("TEMP", "."), "picme_test_ingest.log"), "ab")
server = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--port", str(PORT)],
                          env=env, stdout=log, stderr=subprocess.STDOUT)
try:
    for _ in range(180):
        try:
            if requests.get(f"{BASE}/api/status", timeout=2).status_code == 200:
                break
        except requests.RequestException:
            time.sleep(1)
    st = requests.get(f"{BASE}/api/status", timeout=10).json()
    check("the server sees the photo", st["photos"] >= 1, st)
    check("the server sees the faces", st["faces"] >= 1, st)

    with open(SELFIE, "rb") as fh:
        selfie = fh.read()
    r = requests.post(f"{BASE}/api/match",
                      files={"file": ("selfie.jpg", selfie, "image/jpeg")}, timeout=120)
    check("the selfie was accepted", r.status_code == 200, r.text[:300])
    data = r.json() if r.status_code == 200 else {}
    check("it found our photo", any(p["id"] == PHOTO_ID for p in data.get("photos", [])),
          data.get("photos"))
    token = data.get("token")
    check("we got a token back", bool(token))
    if token:
        r = requests.get(f"{BASE}/api/thumb/{PHOTO_ID}", params={"t": token}, timeout=30)
        check("the thumbnail downloads with that token -> 200", r.status_code == 200, r.status_code)
        r = requests.get(f"{BASE}/api/photo/{PHOTO_ID}", params={"t": token}, timeout=60)
        check("the full photo downloads with that token -> 200", r.status_code == 200, r.status_code)
finally:
    server.terminate()
    try:
        server.wait(timeout=15)
    except subprocess.TimeoutExpired:
        server.kill()

print("\n== cleanup ==")
with db.connect() as con, con.cursor() as cur:
    cur.execute("delete from photos where id = %s", (PHOTO_ID,))   # faces cascade
    cur.execute("delete from sessions where photo_ids = %s", ([PHOTO_ID],))
thumb = rows[0][2] if rows and rows[0][2] else None
if thumb:
    storage.delete(thumb)
for f in (f"photos/{PHOTO_NAME}", f"data/thumbs/local_{PHOTO_NAME}.jpg", SELFIE):
    if os.path.exists(f):
        os.remove(f)
print("cleaned up")

print()
if FAILED:
    print(f"INGEST TESTS FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("Ingest + match OK")
