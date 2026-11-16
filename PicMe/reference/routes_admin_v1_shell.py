"""REFERENCE ONLY - the ORIGINAL admin file, exactly as it was first created
(Phase 0, step 4 of the split), before it was replaced by the full admin API.

This file is NOT imported by the app - nothing in PicMe references it. It is
kept purely so you can read the "before" version side by side with the live
one in routes_admin.py.

    live version  : routes_admin.py              (GET /admin/exports,
                                                    POST /admin/exports/{id}/regenerate,
                                                    POST /admin/exports/{id}/expire,
                                                    every endpoint checks ADMIN_PASSWORD)
    this version  : reference/routes_admin_v1_shell.py   (empty shell - endpoints came later)

Nothing here is served or executed - it is a snapshot for reading only.
"""
from fastapi import APIRouter

router = APIRouter()
