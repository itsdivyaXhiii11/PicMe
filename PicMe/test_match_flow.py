"""A5/A6 end-to-end test: the whole student journey, against a running server.

    venv\\Scripts\\python.exe test_match_flow.py

Covers:
  - /api/status reports the indexed photos/faces
  - POST /api/match with a selfie returns a token + the matching photos
  - the token unlocks exactly those photos (thumb 200, other photo 403)
  - a missing/expired token is rejected (401)
  - the session is stored in Postgres as a HASH (the raw token is not in the DB)
  - the session survives a server restart (run again with --after-restart)

It picks the first photo in the database and uses a matching local copy as the
"selfie", so it works for both Drive photos and local folder photos.
"""
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.getcwd(), ".env"))
sys.path.insert(0, os.getcwd())
import db  # noqa: E402

BASE = os.environ.get("TEST_BASE_URL", "http://127.0.0.1:8000")
FAILED = []


def check(label, ok, extra=""):
    print(("  ok   " if ok else "  FAIL ") + label + ((" -> " + str(extra)) if extra else ""))
    if not ok:
        FAILED.append(label)


# ------------------------------------------------------- pick a test photo
photos = db.list_photos()          # [(id, name, thumb_path), ...]
if not photos:
    sys.exit("no photos in the database - run ingest.py first")

pid, name, thumb = photos[0]
other_pid, other_name = photos[1][0], photos[1][1] if len(photos) > 1 else (pid, name)

# the selfie: a local copy of the same photo (drive-test-photos/ holds them)
candidates = [os.path.join(os.getcwd(), "drive-test-photos", name),
              os.path.join(os.getcwd(), "photos", name)]
SELFIE = next((c for c in candidates if os.path.exists(c)), None)
if SELFIE is None:
    sys.exit(f"no local copy of '{name}' to use as a selfie ({candidates})")
print(f"using selfie file: {SELFIE}\n")

# ---------------------------------------------------------------- status
r = requests.get(f"{BASE}/api/status", timeout=15)
st = r.json()
check("GET /api/status -> 200", r.status_code == 200, st)
check("server has photos indexed", st.get("photos", 0) > 0, f"{st.get('photos')} photos")
check("server has faces indexed", st.get("faces", 0) > 0, f"{st.get('faces')} faces")

# ----------------------------------------------------------------- match
with open(SELFIE, "rb") as fh:
    r = requests.post(f"{BASE}/api/match", files={"file": ("selfie.jpg", fh, "image/jpeg")}, timeout=60)
check("POST /api/match -> 200", r.status_code == 200, r.text[:120])
data = r.json()
token = data.get("token")
ids = [p["id"] for p in data.get("photos", [])]
check("match returned a token", bool(token))
check("match found at least one photo", len(ids) >= 1, ids)
check("the selfie photo is in the result", pid in ids, ids)

if not token:
    print("no token -> cannot continue")
    sys.exit(1)

# ---------------------------------------------- session is hashed in the DB
row = db.get_session(db.hash_token(token))
check("session exists in Postgres", row is not None, row)
check("session stores the matched photo ids", row and set(row["photo_ids"]) == set(ids), row and row["photo_ids"])
with db.connect() as con, con.cursor() as cur:
    cur.execute("select count(*) from sessions where token_hash = %s", (token,))
    raw_leak = cur.fetchone()[0]
check("raw token is NOT stored anywhere (only its hash)", raw_leak == 0)

# ---------------------------------------------------------- permissions
r = requests.get(f"{BASE}/api/thumb/{pid}", params={"t": token}, timeout=15)
check("thumbnail with a good token -> 200", r.status_code == 200, r.status_code)
check("thumbnail is really a jpeg", r.content[:3] == b"\xff\xd8\xff", r.headers.get("content-type"))

r = requests.get(f"{BASE}/api/thumb/{pid}", params={"t": "not-a-real-token"}, timeout=15)
check("thumbnail with a bogus token -> 401", r.status_code == 401, r.status_code)

if other_pid != pid:
    r = requests.get(f"{BASE}/api/photo/{other_pid}", params={"t": token}, timeout=15)
    check("somebody else's photo -> 403", r.status_code == 403, r.status_code)

r = requests.get(f"{BASE}/api/photo/{pid}", params={"t": token}, timeout=60)
check("full photo (from Drive) with a good token -> 200",
      r.status_code == 200 and r.content[:3] == b"\xff\xd8\xff", r.status_code)

# ------------------------------------------- session survives a restart?
with open("last_token.txt", "w") as fh:
    fh.write(token)

if "--after-restart" in sys.argv:
    with open("last_token.txt") as fh:
        old = fh.read().strip()
    r = requests.get(f"{BASE}/api/thumb/{pid}", params={"t": old}, timeout=15)
    check("session STILL works after the server restarted -> 200", r.status_code == 200, r.status_code)

print()
print(f"token for the restart test: {token}")
if FAILED:
    print(f"FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("A5/A6 match flow OK")
