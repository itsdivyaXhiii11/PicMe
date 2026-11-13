"""A4 - Supabase Storage (the thumbnail bucket).

We talk to the Storage REST API directly with `requests`, so the project
keeps its dependency list small and there is nothing new to install.

The bucket is PRIVATE by default: a thumbnail cannot be read without a
link (a signed URL, or the service key). That matters - these are photos
of real students.

Functions:
    ensure_bucket()             -> create the bucket if it does not exist yet
    upload(path, data)          -> store bytes, returns the object path
    exists(path)                -> True/False
    download(path)              -> bytes (None if missing)
    delete(path)                -> remove an object
    signed_url(path, seconds)   -> a temporary read-only link
    list_objects(prefix)        -> what is in the bucket
"""
import os
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
BUCKET = os.environ.get("STORAGE_BUCKET", "thumbs")
TIMEOUT = 120  # uploading a few hundred thumbnails over the event wifi


def _headers(extra=None):
    h = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"}
    if extra:
        h.update(extra)
    return h


def _url(*parts):
    """SUPABASE_URL + path, with the object path URL-encoded."""
    return "/".join([SUPABASE_URL] + [quote(p.strip("/"), safe="/") for p in parts])


def _check_config():
    if not SUPABASE_URL or not SERVICE_KEY:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY missing - fill them in .env")


def ensure_bucket(bucket=BUCKET):
    """Create the bucket if it is not there yet. Safe to call every time."""
    _check_config()
    r = requests.get(_url("storage/v1/bucket", bucket), headers=_headers(), timeout=TIMEOUT)
    if r.status_code == 200:
        return bucket
    r = requests.post(_url("storage/v1/bucket"),
                      headers=_headers({"Content-Type": "application/json"}),
                      json={"id": bucket, "name": bucket, "public": False},
                      timeout=TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"could not create bucket '{bucket}': {r.status_code} {r.text[:200]}")
    return bucket


def upload(path, data, bucket=BUCKET, content_type="image/jpeg"):
    """Store bytes under `path` (overwrite if it is already there)."""
    _check_config()
    r = requests.post(_url("storage/v1/object", bucket, path),
                      headers=_headers({"Content-Type": content_type, "x-upsert": "true"}),
                      data=data, timeout=TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"upload failed for '{path}': {r.status_code} {r.text[:200]}")
    return path


def exists(path, bucket=BUCKET):
    _check_config()
    r = requests.head(_url("storage/v1/object", bucket, path), headers=_headers(), timeout=TIMEOUT)
    return r.status_code == 200


def download(path, bucket=BUCKET):
    """-> bytes, or None when the object is not there."""
    _check_config()
    r = requests.get(_url("storage/v1/object", bucket, path), headers=_headers(), timeout=TIMEOUT)
    return r.content if r.status_code == 200 else None


def delete(path, bucket=BUCKET):
    _check_config()
    r = requests.delete(_url("storage/v1/object", bucket, path), headers=_headers(), timeout=TIMEOUT)
    return r.status_code < 400


def signed_url(path, expires_in=3600, bucket=BUCKET):
    """A temporary link to one object. Returns None when Storage refuses."""
    _check_config()
    r = requests.post(_url("storage/v1/object/sign", bucket, path),
                      headers=_headers({"Content-Type": "application/json"}),
                      json={"expiresIn": int(expires_in)}, timeout=TIMEOUT)
    if r.status_code >= 400:
        return None
    return SUPABASE_URL + r.json()["signedURL"]


def list_objects(prefix="", bucket=BUCKET, limit=1000):
    """-> [(name, size), ...] for everything in the bucket.
    The prefix is filtered here (the Storage API ignores it on POST)."""
    _check_config()
    r = requests.post(_url("storage/v1/object/list", bucket),
                      headers=_headers({"Content-Type": "application/json"}),
                      json={"prefix": "", "limit": limit}, timeout=TIMEOUT)
    if r.status_code >= 400:
        return []
    out = []
    for o in r.json():
        name = o.get("name", "")
        if prefix and not name.startswith(prefix):
            continue
        size = o.get("size") or (o.get("metadata") or {}).get("size")
        out.append((name, size))
    return out
