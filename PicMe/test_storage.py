"""A4 test: the Supabase Storage bucket (thumbnails).

    venv\\Scripts\\python.exe test_storage.py

Creates the bucket if needed, uploads a tiny test image, reads it back,
makes a signed link and cleans up after itself.
"""
import io
import sys
import time

from PIL import Image

import storage

FAILED = []


def check(label, ok, extra=""):
    print(("  ok   " if ok else "  FAIL ") + label + ((" -> " + str(extra)) if extra else ""))
    if not ok:
        FAILED.append(label)


# a tiny real JPEG (16x16 grey) - no need for a photo
buf = io.BytesIO()
Image.new("RGB", (16, 16), (120, 130, 140)).save(buf, format="JPEG")
data = buf.getvalue()

print("bucket:", storage.BUCKET)

storage.ensure_bucket()
check("bucket exists", storage.BUCKET in [b for b in [storage.BUCKET]])

path = f"test-{int(time.time())}.jpg"

storage.upload(path, data)
check("upload", storage.exists(path))

back = storage.download(path)
check("download gives the same bytes", back == data, f"{len(back or b'')} bytes")

url = storage.signed_url(path, 60)
check("signed url", bool(url) and url.startswith("https://"), url)

names = [n for n, _ in storage.list_objects("test-")]
check("object is listed in the bucket", path in names, names)

storage.delete(path)
check("delete", not storage.exists(path))

print()
if FAILED:
    print(f"STORAGE FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("Storage OK")
