"""Person A - admin API (split out of app.py).

Endpoints (every one of them checks ADMIN_PASSWORD):

  GET  /admin/exports                    -> list the exports
  POST /admin/exports                    -> create one (photo ids + part number)
  POST /admin/exports/{id}/regenerate    -> a fresh download link
  POST /admin/exports/{id}/expire        -> mark an export expired

The password can be sent two ways:
  curl -H "X-Admin-Password: secret" http://host:8000/admin/exports
  curl "http://host:8000/admin/exports?password=secret"

The exports table stores ONLY photo ids and a part number - never a link.
A link is built on demand: regenerate() mints a fresh session token for
those photo ids and hands back a URL, exactly like a student's own link.
"""
import os
import secrets
import time
import uuid

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

import db

router = APIRouter()

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
"""checked on EVERY /admin/* endpoint"""
EXPORT_LINK_TTL = int(os.environ.get("EXPORT_LINK_TTL", "86400"))
"""how long a regenerated link stays valid (24h)"""
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
"""the address students reach the site on - only used to build links"""


# ------------------------------------------------------------------- auth
def require_admin(x_admin_password: str = Header(default=None),
                  password: str = Query(default=None)):
    """Reject the request unless the admin password matches."""
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "ADMIN_PASSWORD is not set on the server")
    given = x_admin_password if x_admin_password is not None else (password or "")
    if not secrets.compare_digest(given.encode(), ADMIN_PASSWORD.encode()):
        raise HTTPException(401, "Wrong admin password")


class ExportIn(BaseModel):
    photo_ids: list[str] = Field(default_factory=list)
    part_no: int | None = None
    expires_in: int | None = Field(default=None, description="seconds; default = 24h")
    id: str | None = None


def _link(token: str) -> str:
    return f"{PUBLIC_URL}/api/zip?t={token}" if PUBLIC_URL else f"/api/zip?t={token}"


def _iso(ts):
    """unix seconds -> '2026-11-12 14:03:22' (empty string when unset)."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)) if ts else None


def _public(row: dict) -> dict:
    """The export as the admin sees it (no token hash, no secrets)."""
    return {"id": row["id"], "photo_ids": row["photo_ids"], "part_no": row["part_no"],
            "created_at": _iso(row["created_at"]), "expires_at": _iso(row["expires_at"])}


# ----------------------------------------------------------------- routes
@router.get("/admin/exports")
def list_exports(x_admin_password: str = Header(default=None),
                 password: str = Query(default=None)):
    require_admin(x_admin_password, password)
    rows = db.list_exports()
    return {"count": len(rows), "exports": [_public(r) for r in rows]}


@router.post("/admin/exports")
def create_export(body: ExportIn,
                  x_admin_password: str = Header(default=None),
                  password: str = Query(default=None)):
    require_admin(x_admin_password, password)
    export_id = body.id or uuid.uuid4().hex[:12]
    expires_at = time.time() + (body.expires_in or EXPORT_LINK_TTL)
    db.save_export(export_id, body.photo_ids, body.part_no, expires_at)
    row = db.get_export(export_id)
    return {"ok": True, "export": _public(row) if row else None}


@router.post("/admin/exports/{export_id}/regenerate")
def regenerate(export_id: str,
               x_admin_password: str = Header(default=None),
               password: str = Query(default=None)):
    """Mint a fresh download link for this export."""
    require_admin(x_admin_password, password)
    row = db.get_export(export_id)
    if row is None:
        raise HTTPException(404, "No such export")
    if row["expires_at"] and row["expires_at"] < time.time():
        raise HTTPException(410, "This export is expired")

    token = secrets.token_urlsafe(24)
    expires_at = time.time() + EXPORT_LINK_TTL
    if row.get("token_hash"):
        db.delete_session(row["token_hash"])   # the previous link stops working
    db.save_session(db.hash_token(token), row["photo_ids"], expires_at)
    db.set_export_token(export_id, db.hash_token(token))
    return {"ok": True, "id": export_id, "token": token, "url": _link(token),
            "photo_ids": row["photo_ids"], "part_no": row["part_no"],
            "expires_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(expires_at))}


@router.post("/admin/exports/{export_id}/expire")
def expire(export_id: str,
           x_admin_password: str = Header(default=None),
           password: str = Query(default=None)):
    """Mark an export expired and kill the link we handed out for it."""
    require_admin(x_admin_password, password)
    row = db.get_export(export_id)
    if row is None:
        raise HTTPException(404, "No such export")
    row = db.expire_export(export_id)
    if row and row.get("token_hash"):
        db.delete_session(row["token_hash"])     # the old link stops working
    return {"ok": True, "export": _public(row) if row else None}
