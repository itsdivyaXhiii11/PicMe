"""Index photos. Resumable: re-run any time to add new photos.

  python ingest.py          -> Google Drive (if DRIVE_FOLDER_ID + service_account.json exist), else ./photos
  python ingest.py --local  -> force the local ./photos folder (great for testing)
"""
import io, os, sys
import cv2
import numpy as np
from PIL import Image, ImageOps
from insightface.app import FaceAnalysis
from common import FOLDER_ID, KEY_FILE, LOCAL_DIR, THUMBS, db, drive, fetch_bytes

EXT = (".jpg", ".jpeg", ".png")


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


def main():
    use_local = "--local" in sys.argv or not (FOLDER_ID and os.path.exists(KEY_FILE))
    svc = None if use_local else drive()
    print("Source:", f"local folder '{LOCAL_DIR}/'" if use_local else "Google Drive")
    con = db()
    seen = {r[0] for r in con.execute("SELECT id FROM photos")}
    fa = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
    fa.prepare(ctx_id=-1, det_size=(960, 960))

    src = list_local() if use_local else list_drive(svc, FOLDER_ID)
    files = [f for f in src if f["id"] not in seen]
    print(f"{len(files)} new photos to process")

    total_faces = 0
    for i, f in enumerate(files, 1):
        try:
            img = ImageOps.exif_transpose(Image.open(io.BytesIO(fetch_bytes(svc, f["id"])))).convert("RGB")
            thumb = img.copy()
            thumb.thumbnail((600, 600))
            thumb.save(f"{THUMBS}/{f['id'].replace('/', '_').replace(':', '_')}.jpg", quality=80)

            big = img.copy()
            big.thumbnail((1600, 1600))
            for fc in fa.get(cv2.cvtColor(np.array(big), cv2.COLOR_RGB2BGR)):
                if fc.det_score < 0.5:
                    continue
                con.execute("INSERT INTO faces VALUES(?,?)",
                            (f["id"], fc.normed_embedding.astype(np.float32).tobytes()))
                total_faces += 1
            con.execute("INSERT INTO photos VALUES(?,?)", (f["id"], f["name"]))
            con.commit()
        except Exception as e:
            print("skipped", f["name"], "->", e)
        if i % 20 == 0 or i == len(files):
            print(f"{i}/{len(files)}  faces found so far: {total_faces}")
    print("Done. Now (re)start the server: uvicorn app:app --port 8000")


if __name__ == "__main__":
    main()
