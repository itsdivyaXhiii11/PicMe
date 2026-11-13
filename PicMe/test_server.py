"""End-to-end test of the server: A5 (index from Postgres), A6 (sessions in
the DB), A7 (rate limit + forwarded IPs), A8 (admin API).

    venv\\Scripts\\python.exe test_server.py

It starts the real server twice (to prove a session survives a restart),
talks to it over HTTP, and cleans up after itself.
"""
import io
import os
import subprocess
import sys
import time

import requests
from PIL import Image

import db

PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"
SERVER_LOG = os.path.join(os.environ.get("TEMP", "."), "picme_test_server.log")
PHOTO_ID = "local:test_session.jpg"
PHOTO_NAME = "test_session.jpg"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

FAILED = []


def check(label, ok, extra=""):
    print(("  ok   " if ok else "  FAIL ") + label + ((" -> " + str(extra)) if extra else ""))
    if not ok:
        FAILED.append(label)


def make_photo_files():
    """A real (tiny) photo on disk, so the download routes can serve it."""
    os.makedirs("photos", exist_ok=True)
    os.makedirs("data/thumbs", exist_ok=True)
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 120, 90)).save(buf, format="JPEG")
    data = buf.getvalue()
    with open(f"photos/{PHOTO_NAME}", "wb") as fh:
        fh.write(data)
    # routes_download.thumb_file() builds <pid with / and : replaced by _> + ".jpg"
    thumb = f"data/thumbs/{PHOTO_ID.replace('/', '_').replace(':', '_')}.jpg"
    with open(thumb, "wb") as fh:
        fh.write(data)
    return thumb


THUMB_FILE = make_photo_files()


def start_server():
    env = dict(os.environ)
    env.update({"PROXY_HOPS": "1", "MAX_TRIES": "2", "WINDOW": "600"})
    log = open(SERVER_LOG, "ab")
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--port", str(PORT)],
                         env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(120):                       # the model takes a few seconds to load
        try:
            if requests.get(f"{BASE}/api/status", timeout=2).status_code == 200:
                return p
        except requests.RequestException:
            time.sleep(1)
    p.kill()
    raise RuntimeError("server did not start, see " + SERVER_LOG)


def stop_server(p):
    p.terminate()
    try:
        p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        p.kill()


print("== setting up test data ==")
make_photo_files()
db.add_photo(PHOTO_ID, PHOTO_NAME, "test-thumb.jpg")
if not db.list_faces():
    import numpy as np
    db.add_face(PHOTO_ID, np.random.rand(512).astype(np.float32))

token = "test-token-1234567890"
db.save_session(db.hash_token(token), [PHOTO_ID], time.time() + 3600)

server = start_server()
try:
    print("\n== A5: the index comes from Postgres ==")
    st = requests.get(f"{BASE}/api/status", timeout=10).json()
    check("/api/status sees the photo we put in the DB", st["photos"] >= 1, st)
    check("/api/status sees the face we put in the DB", st["faces"] >= 1, st)
    check("/api/status reports the threshold", "threshold" in st, st)

    print("\n== A6: sessions live in the DB ==")
    r = requests.get(f"{BASE}/api/thumb/{PHOTO_ID}", params={"t": token}, timeout=10)
    check("thumb with a valid token -> 200", r.status_code == 200, r.status_code)
    r = requests.get(f"{BASE}/api/thumb/{PHOTO_ID}", params={"t": "wrong"}, timeout=10)
    check("thumb with a wrong token -> 401", r.status_code == 401, r.status_code)
    r = requests.get(f"{BASE}/api/zip", params={"t": token}, timeout=30)
    check("zip with a valid token -> 200", r.status_code == 200, r.status_code)
    check("zip really is a zip", r.content[:2] == b"PK", r.content[:8])
    r = requests.get(f"{BASE}/api/photo/{PHOTO_ID}", params={"t": token}, timeout=30)
    check("full photo with a valid token -> 200", r.status_code == 200, r.status_code)
    r = requests.get(f"{BASE}/api/photo/{PHOTO_ID}", params={"t": token}, timeout=30)
    check("photo of another id is refused -> 403",
          requests.get(f"{BASE}/api/photo/local:nope.jpg", params={"t": token},
                       timeout=10).status_code == 403)

    print("\n== A7: the rate limit counts forwarded IPs separately ==")
    garbage = {"file": ("x.jpg", b"not-an-image", "image/jpeg")}
    h = {"X-Forwarded-For": "1.2.3.4"}
    codes = [requests.post(f"{BASE}/api/match", files=garbage, headers=h, timeout=30).status_code
             for _ in range(3)]
    check("same IP is limited (429 after 2 tries)", 429 in codes, codes)
    codes = [requests.post(f"{BASE}/api/match", files=garbage,
                           headers={"X-Forwarded-For": "9.9.9.9"}, timeout=30).status_code
             for _ in range(3)]
    check("a different forwarded IP has its own counter", 429 in codes, codes)

    print("\n== A8: the admin API ==")
    r = requests.get(f"{BASE}/admin/exports", timeout=10)
    check("no password -> 401", r.status_code == 401, r.status_code)
    r = requests.get(f"{BASE}/admin/exports", params={"password": "nope"}, timeout=10)
    check("wrong password -> 401", r.status_code == 401, r.status_code)
    r = requests.get(f"{BASE}/admin/exports", headers={"X-Admin-Password": ADMIN_PASSWORD},
                     timeout=10)
    check("right password -> 200", r.status_code == 200, r.status_code)
    check("the list has exports", "exports" in r.json(), r.text[:200])

    r = requests.post(f"{BASE}/admin/exports", headers={"X-Admin-Password": ADMIN_PASSWORD},
                      json={"photo_ids": [PHOTO_ID], "part_no": 1}, timeout=10)
    check("create an export -> 200", r.status_code == 200, r.text[:200])
    export_id = r.json()["export"]["id"]

    links = []
    for i in range(2):
        r = requests.post(f"{BASE}/admin/exports/{export_id}/regenerate",
                          headers={"X-Admin-Password": ADMIN_PASSWORD}, timeout=10)
        check(f"regenerate #{i + 1} -> 200", r.status_code == 200, r.text[:200])
        links.append(r.json()["token"])
    check("the two links are different", links[0] != links[1])
    r = requests.get(f"{BASE}/api/zip", params={"t": links[-1]}, timeout=30)
    check("the regenerated link downloads -> 200", r.status_code == 200, r.status_code)

    r = requests.get(f"{BASE}/api/zip", params={"t": links[0]}, timeout=30)
    check("the old link is dead after a regenerate -> 401", r.status_code == 401, r.status_code)
    r = requests.post(f"{BASE}/admin/exports/{export_id}/expire",
                      headers={"X-Admin-Password": ADMIN_PASSWORD}, timeout=10)
    check("expire -> 200", r.status_code == 200, r.text[:200])
    r = requests.get(f"{BASE}/api/zip", params={"t": links[1]}, timeout=30)
    check("the link is dead after expire -> 401", r.status_code == 401, r.status_code)
    r = requests.post(f"{BASE}/admin/exports/{export_id}/regenerate",
                      headers={"X-Admin-Password": ADMIN_PASSWORD}, timeout=10)
    check("regenerating an expired export -> 410", r.status_code == 410, r.status_code)
    r = requests.post(f"{BASE}/admin/exports/no-such-export/expire",
                      headers={"X-Admin-Password": ADMIN_PASSWORD}, timeout=10)
    check("unknown export -> 404", r.status_code == 404, r.status_code)
finally:
    stop_server(server)

print("\n== A6: the session survives a restart ==")
server = start_server()
try:
    r = requests.get(f"{BASE}/api/thumb/{PHOTO_ID}", params={"t": token}, timeout=10)
    check("the old session still works after a restart -> 200", r.status_code == 200, r.status_code)
finally:
    stop_server(server)

print("\n== cleanup ==")
with db.connect() as con, con.cursor() as cur:
    cur.execute("delete from photos where id = %s", (PHOTO_ID,))
    cur.execute("delete from sessions where token_hash = %s", (db.hash_token(token),))
    cur.execute("delete from exports where photo_ids = %s", ([PHOTO_ID],))
for f in (f"photos/{PHOTO_NAME}", THUMB_FILE):
    if os.path.exists(f):
        os.remove(f)

print()
if FAILED:
    print(f"SERVER TESTS FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("All server tests OK")
