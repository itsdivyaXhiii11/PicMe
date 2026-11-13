"""A4 - Index photos into Supabase.

    python ingest.py            -> Google Drive (if DRIVE_FOLDER_ID + service_account.json exist), else ./photos
    python ingest.py --local    -> force the local ./photos folder (great for testing)

What it does, per photo:
  1. download the original (Drive or local file)
  2. make a 600px thumbnail, upload it to the Supabase Storage bucket
     and remember its path in photos.thumb_path
  3. detect the faces with InsightFace and store the 512 numbers in Postgres

Resumable: photos that are already in the photos table are skipped, so you
can stop it at any time and run it again. A photo that fails is printed and
skipped - the rest of the event keeps going.
"""
import hashlib
import io
import os
import sys

import cv2
import numpy as np
from PIL import Image, ImageOps
from insightface.app import FaceAnalysis

import db
import storage
from common import FOLDER_ID, KEY_FILE, LOCAL_DIR, THUMBS, drive, fetch_bytes

EXT = (".jpg", ".jpeg", ".png")
THUMB_SIZE = 600       # pixels on the long side
DETECT_SIZE = 960      # bigger input = better face detection


def list_drive(svc, folder):
    token = None
    while True:
        r = svc.files().list(
            q=f"'{folder}' in parents and trashed=false",
            fields="nextPageToken, files(id,name,mimeType)", pageSize=1000, pageToken=token,
            supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        for f in r["files"]:
            if f["mimeType"] == "application/vnd.google-apps.folder":
                yield from list_drive(svc, f["id"])
            elif f["mimeType"] in ("image/jpeg", "image/png"):
                yield f
        token = r.get("nextPageToken")
        if not token:
            break


def list_local():
    for root, _, names in os.walk(LOCAL_DIR):
        for n in sorted(names):
            if n.lower().endswith(EXT):
                rel = os.path.relpath(os.path.join(root, n), LOCAL_DIR)
                yield {"id": "local:" + rel, "name": n}


def safe_name(n):
    """A file name that is safe to use as a Storage object name."""
    return "".join(c if (c.isalnum() or c in "._- ") else "_" for c in n).strip() or "photo.jpg"


def thumb_local_name(pid):
    """Exactly the name routes_download.py expects (data/thumbs/<pid>.jpg)."""
    return f"{THUMBS}/{pid.replace('/', '_').replace(':', '_')}.jpg"


def storage_path(pid, name):
    """A unique object path inside the bucket: <hash>-<safe name>.jpg"""
    h = hashlib.sha1(pid.encode()).hexdigest()[:8]
    return f"{h}-{safe_name(name)}"


def main():
    use_local = "--local" in sys.argv or not (FOLDER_ID and os.path.exists(KEY_FILE))
    svc = None if use_local else drive()
    print("Source:", f"local folder '{LOCAL_DIR}/'" if use_local else "Google Drive")

    storage.ensure_bucket()
    print(f"Storage bucket '{storage.BUCKET}' ready")

    known = db.list_photo_ids()          # {id: thumb_path} - already indexed
    print(f"{len(known)} photos already in the database")

    fa = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
    fa.prepare(ctx_id=-1, det_size=(DETECT_SIZE, DETECT_SIZE))

    src = list_local() if use_local else list_drive(svc, FOLDER_ID)
    files = [f for f in src if f["id"] not in known]
    print(f"{len(files)} new photos to process")

    done = skipped = 0
    total_faces = 0
    with db.connect() as con:
        for i, f in enumerate(files, 1):
            pid, name = f["id"], f["name"]
            try:
                raw = fetch_bytes(svc, pid)
                img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")

                # --- thumbnail: local copy (fast local serving) + Storage copy (the real one)
                thumb = img.copy()
                thumb.thumbnail((THUMB_SIZE, THUMB_SIZE))
                buf = io.BytesIO()
                thumb.save(buf, format="JPEG", quality=80)
                data = buf.getvalue()
                os.makedirs(THUMBS, exist_ok=True)
                with open(thumb_local_name(pid), "wb") as fh:
                    fh.write(data)
                try:
                    obj = storage.upload(storage_path(pid, name), data)
                except Exception as e:            # storage hiccup: keep going, photo row stays
                    print("  ! thumb upload failed:", e)
                    obj = None

                # --- faces
                big = img.copy()
                big.thumbnail((1600, 1600))
                faces = fa.get(cv2.cvtColor(np.array(big), cv2.COLOR_RGB2BGR))

                # the photo row first: faces.photo_id points at it
                db.add_photo(pid, name, obj, con=con)
                n = 0
                for fc in faces:
                    if fc.det_score < 0.5:
                        continue
                    db.add_face(pid, fc.normed_embedding.astype(np.float32), con=con)
                    n += 1
                con.commit()
                total_faces += n
                done += 1
            except Exception as e:
                con.rollback()
                skipped += 1
                print("skipped", name, "->", e)
            if i % 20 == 0 or i == len(files):
                print(f"{i}/{len(files)}  faces so far: {total_faces}")

    print()
    print(f"indexed {done} photos, skipped {skipped} failures, {total_faces} faces")
    print(f"database now holds {db.photo_count()} photos / {db.face_count()} faces")
    print("Now (re)start the server: uvicorn main:app --port 8000")


if __name__ == "__main__":
    main()
