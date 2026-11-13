"""A8 test: the admin API (needs a running server).

    venv\\Scripts\\python.exe test_admin_api.py

The admin password is read straight from .env - it never gets printed.
Covers:
  - a request without / with the wrong password is rejected (401)
  - GET /admin/exports lists the exports
  - POST /admin/exports creates one
  - POST /admin/exports/{id}/regenerate returns a WORKING link, twice in a row
  - POST /admin/exports/{id}/expire marks it expired and kills the link
  - regenerating an expired export is refused (410)
"""
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.getcwd(), ".env"))
sys.path.insert(0, os.getcwd())
import db  # noqa: E402

BASE = os.environ.get("TEST_BASE_URL", "http://127.0.0.1:8000")
PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
FAILED = []


def check(label, ok, extra=""):
    print(("  ok   " if ok else "  FAIL ") + label + ((" -> " + str(extra)) if extra else ""))
    if not ok:
        FAILED.append(label)


if not PASSWORD:
    sys.exit("ADMIN_PASSWORD missing from .env")

photos = [p[0] for p in db.list_photos()][:2]
if not photos:
    sys.exit("no photos in the database - run ingest.py first")

# ----------------------------------------------------------- wrong password
r = requests.get(f"{BASE}/admin/exports", timeout=15)
check("no password -> 401", r.status_code == 401, r.status_code)
r = requests.get(f"{BASE}/admin/exports", params={"password": "totally-wrong"}, timeout=15)
check("wrong password -> 401", r.status_code == 401, r.status_code)

# ------------------------------------------------------------- with password
h = {"X-Admin-Password": PASSWORD}
r = requests.get(f"{BASE}/admin/exports", headers=h, timeout=15)
check("right password -> 200", r.status_code == 200, r.status_code)
check("list returns the exports array", isinstance(r.json().get("exports"), list), r.json().get("count"))

# -------------------------------------------------------------- create one
export_id = "testexp1"
r = requests.post(f"{BASE}/admin/exports", headers=h,
                  json={"photo_ids": photos, "part_no": 1, "id": export_id}, timeout=15)
check("create export -> 200", r.status_code == 200, r.text[:150])
check("created export has our photo ids", r.json().get("export", {}).get("photo_ids") == photos,
      r.json().get("export", {}).get("photo_ids"))

# ------------------------------------------- regenerate twice, both work
links = []
for n in (1, 2):
    r = requests.post(f"{BASE}/admin/exports/{export_id}/regenerate", headers=h, timeout=15)
    check(f"regenerate #{n} -> 200", r.status_code == 200, r.text[:150])
    body = r.json()
    check(f"regenerate #{n} returns a token + url", bool(body.get("token")) and bool(body.get("url")))
    links.append(body.get("token"))
    # does the link actually work?
    dl = requests.get(f"{BASE}/api/zip", params={"t": body.get("token")}, timeout=60)
    check(f"regenerated link #{n} downloads a real zip",
          dl.status_code == 200 and dl.content[:2] == b"PK",
          f"{dl.status_code}, {len(dl.content)} bytes")
check("the two links are different tokens", links[0] and links[1] and links[0] != links[1])

# ---------------------------------------------------------------- expire
r = requests.post(f"{BASE}/admin/exports/{export_id}/expire", headers=h, timeout=15)
check("expire -> 200", r.status_code == 200, r.text[:150])
r = requests.post(f"{BASE}/admin/exports/{export_id}/regenerate", headers=h, timeout=15)
check("regenerate an expired export -> 410", r.status_code == 410, r.status_code)

# ---------------------------------------------------------------- cleanup
with db.connect() as con, con.cursor() as cur:
    cur.execute("delete from exports where id = %s", (export_id,))
    cur.execute("delete from sessions where token_hash in (%s, %s)",
                (db.hash_token(links[0] or "x"), db.hash_token(links[1] or "y")))

print()
if FAILED:
    print(f"FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("A8 admin API OK")
