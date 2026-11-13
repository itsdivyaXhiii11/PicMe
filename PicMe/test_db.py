"""A3 test: exercise EVERY helper in db.py once.

    venv\\Scripts\\python.exe test_db.py

Uses fake 'test:' rows and deletes them at the end, so real data is untouched.
"""
import sys
import time

import numpy as np

import db

FAILED = []


def check(label, ok, extra=""):
    print(("  ok   " if ok else "  FAIL ") + label + ((" -> " + str(extra)) if extra else ""))
    if not ok:
        FAILED.append(label)


# ---- start from a clean slate (fake rows from a previous run) ----
with db.connect() as con, con.cursor() as cur:
    cur.execute("delete from photos where id like 'test:%'")   # faces cascade
    cur.execute("delete from sessions where token_hash like 'test%'")

# ---- photos ----
db.add_photo("test:p1", "TEST_one.jpg")
db.add_photo("test:p2", "TEST_two.jpg")
db.add_photo("test:p1", "TEST_one.jpg")          # duplicate: must be ignored
photos = db.list_photos()
test_ids = [p[0] for p in photos]
check("add_photo + list_photos (duplicate skipped)", test_ids.count("test:p1") == 1 and "test:p2" in test_ids,
      f"{len(photos)} photos in table")

# ---- faces: 512 numbers in, 512 numbers out ----
e1 = np.random.rand(512).astype(np.float32)
e2 = np.random.rand(512).astype(np.float32)
db.add_face("test:p1", e1)
db.add_face("test:p2", e2)
faces = [f for f in db.list_faces() if f[0].startswith("test:")]
check("add_face + list_faces (count)", len(faces) == 2, f"{len(faces)} test faces")
back = next((np.array(f[1], dtype=np.float32) for f in faces if f[0] == "test:p1"), None)
check("embedding round-trip (512 floats survive)", back is not None and np.allclose(back, e1))

# ---- sessions ----
h = db.hash_token("my-secret-token")
check("hash_token is one-way + stable", h == db.hash_token("my-secret-token") and h != "my-secret-token")

db.save_session(h, ["test:p1", "test:p2"], time.time() + 3600)
s = db.get_session(h)
check("save_session + get_session", s is not None and s["photo_ids"] == ["test:p1", "test:p2"],
      s)
check("expiry is in the future", s is not None and s["expires_at"] > time.time())
check("get_session(unknown) -> None", db.get_session("nope") is None)

db.save_session(db.hash_token("already-gone"), ["test:p1"], time.time() - 5)  # born expired
gone = db.hash_token("already-gone")
removed = db.delete_expired_sessions()
check("delete_expired_sessions removes the dead one", removed >= 1 and db.get_session(gone) is None,
      f"{removed} removed")
check("...but keeps the live one", db.get_session(h) is not None)

# ---- cleanup ----
with db.connect() as con, con.cursor() as cur:
    cur.execute("delete from photos where id like 'test:%'")
    cur.execute("delete from sessions where token_hash like 'test%'")

print()
if FAILED:
    print(f"A3 FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("A3 all helpers OK")
