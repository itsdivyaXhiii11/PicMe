"""A3 - ALL database access (Supabase / PostgreSQL) lives in this file.

Nothing else in the project talks SQL; every other file calls a helper:

    add_photo(id, name, thumb_path)      <- A4 ingest.py
    add_face(photo_id, embedding)        <- A4 ingest.py
    list_photos()                        <- A5 main.py (build the index)
    list_faces()                         <- A5 main.py (build the index)
    hash_token(token)                    <- A6 sessions (store only the hash!)
    save_session(hash, photo_ids, exp)   <- A6
    get_session(hash)                    <- A6
    delete_expired_sessions()            <- A6 (called when a new one is saved)
    save_export / list_exports / ...     <- A8 admin API

Connections come from a small thread-safe pool: the event server handles a
whole campus at once, and opening a fresh TLS connection per request is
both slow and (on Supabase's free tier) limited to ~15 at a time.

Every helper: get a connection -> do ONE job -> commit -> close.
"""
import hashlib
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
import psycopg2.pool
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL", "")
POOL_SIZE = int(os.environ.get("POOL_SIZE", "5"))

_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    """Create the connection pool once, on first use."""
    global _pool
    with _pool_lock:
        if _pool is None:
            if not DATABASE_URL:
                raise RuntimeError(
                    "DATABASE_URL missing - fill it in .env (see .env.example)")
            _pool = psycopg2.pool.ThreadedConnectionPool(1, POOL_SIZE, DATABASE_URL)
        return _pool


@contextmanager
def connect():
    """Borrow a Postgres connection. Commits on success, rolls back on error,
    ALWAYS gives the connection back. Use:  with db.connect() as con: ..."""
    pool = _get_pool()
    con = pool.getconn()
    try:
        yield con
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        pool.putconn(con, close=True)  # something went wrong: don't reuse it
        raise
    else:
        pool.putconn(con)


def _exec(sql, params=(), con=None):
    """Run one statement. With `con` it joins that transaction, otherwise it
    opens its own short-lived one (and commits)."""
    if con is not None:
        with con.cursor() as cur:
            cur.execute(sql, params)
        return
    with connect() as c, c.cursor() as cur:
        cur.execute(sql, params)


# ---------------------------------------------------------------- tokens
def hash_token(token: str) -> str:
    """SHA-256 fingerprint of a session token. One-way: the database only
    ever holds this, so a database leak cannot be turned back into a token."""
    return hashlib.sha256(token.encode()).hexdigest()


# ----------------------------------------------------------------- photos
def add_photo(pid, name, thumb_path=None, con=None):
    """Store one photo. Re-running is safe: an id we already have is ignored
    (this is what keeps ingest.py resumable)."""
    _exec(
        "insert into photos (id, name, thumb_path) values (%s, %s, %s) "
        "on conflict (id) do update set name = excluded.name, "
        "thumb_path = coalesce(excluded.thumb_path, photos.thumb_path)",
        (pid, name, thumb_path), con)


def list_photos():
    """-> [(id, name, thumb_path), ...] sorted the same way the grid shows."""
    with connect() as con, con.cursor() as cur:
        cur.execute("select id, name, thumb_path from photos order by name, id")
        return cur.fetchall()


def list_photo_ids():
    """-> {id: thumb_path} - what ingest.py uses to skip work it already did."""
    with connect() as con, con.cursor() as cur:
        cur.execute("select id, thumb_path from photos")
        return {r[0]: r[1] for r in cur.fetchall()}


def photo_count():
    with connect() as con, con.cursor() as cur:
        cur.execute("select count(*) from photos")
        return cur.fetchone()[0]


# ------------------------------------------------------------------ faces
def add_face(pid, embedding, con=None):
    """Store one face: the 512 numbers as a plain Postgres float array.
    `embedding` may be a numpy array or any list of numbers."""
    emb = embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)
    _exec("insert into faces (photo_id, embedding) values (%s, %s)", (pid, emb), con)


def list_faces():
    """-> [(photo_id, [512 floats]), ...] - main.py stacks these into MAT."""
    with connect() as con, con.cursor() as cur:
        cur.execute("select photo_id, embedding from faces")
        return cur.fetchall()


def face_count():
    with connect() as con, con.cursor() as cur:
        cur.execute("select count(*) from faces")
        return cur.fetchone()[0]


# --------------------------------------------------------------- sessions
def save_session(token_hash, photo_ids, expires_at):
    """Create (or refresh) a session.
    token_hash : output of hash_token() - never the raw token
    photo_ids  : list of photo ids this student matched
    expires_at : unix seconds, e.g. time.time() + 3600"""
    _exec(
        "insert into sessions (token_hash, photo_ids, expires_at) "
        "values (%s, %s, to_timestamp(%s)) "
        "on conflict (token_hash) do update "
        "set photo_ids = excluded.photo_ids, expires_at = excluded.expires_at",
        (token_hash, list(photo_ids), float(expires_at)))


def get_session(token_hash):
    """-> {"photo_ids": [...], "expires_at": unix seconds}  or  None if unknown."""
    with connect() as con, con.cursor() as cur:
        cur.execute(
            "select photo_ids, extract(epoch from expires_at)::bigint "
            "from sessions where token_hash = %s", (token_hash,))
        row = cur.fetchone()
    if row is None:
        return None
    return {"photo_ids": list(row[0]), "expires_at": int(row[1])}


def delete_session(token_hash):
    """Remove one session (used when an admin export is expired)."""
    with connect() as con, con.cursor() as cur:
        cur.execute("delete from sessions where token_hash = %s", (token_hash,))
        return cur.rowcount


def delete_expired_sessions():
    """Sweep sessions past their expiry. Returns how many rows went away.
    Called every time a new session is created (A6)."""
    with connect() as con, con.cursor() as cur:
        cur.execute("delete from sessions where expires_at < now()")
        return cur.rowcount


# ---------------------------------------------------------------- exports
def save_export(export_id, photo_ids, part_no=None, expires_at=None):
    """Create (or update) one export row. Only ids and a part number are
    stored - never a link. `expires_at` is unix seconds or None."""
    exp = datetime.fromtimestamp(float(expires_at), tz=timezone.utc) if expires_at else None
    _exec(
        "insert into exports (id, photo_ids, part_no, expires_at) "
        "values (%s, %s, %s, %s) "
        "on conflict (id) do update set photo_ids = excluded.photo_ids, "
        "part_no = excluded.part_no, expires_at = excluded.expires_at",
        (export_id, list(photo_ids), part_no, exp))


def list_exports():
    """-> [{id, photo_ids, part_no, created_at, expires_at, token_hash}, ...]

    The timestamps are unix seconds (floats), so they can be compared with
    time.time() directly."""
    with connect() as con, con.cursor() as cur:
        cur.execute(
            "select id, photo_ids, part_no, created_at, expires_at, token_hash "
            "from exports order by created_at desc, id")
        rows = cur.fetchall()
    return [
        {"id": r[0], "photo_ids": list(r[1]), "part_no": r[2],
         "created_at": r[3].timestamp() if r[3] else None,
         "expires_at": r[4].timestamp() if r[4] else None,
         "token_hash": r[5]}
        for r in rows
    ]


def get_export(export_id):
    """-> the same dict as list_exports(), or None when the id is unknown."""
    for e in list_exports():
        if e["id"] == export_id:
            return e
    return None


def set_export_token(export_id, token_hash):
    """Remember which session token was handed out for this export, so that
    expiring the export can also kill that link."""
    _exec("update exports set token_hash = %s where id = %s", (token_hash, export_id))


def expire_export(export_id):
    """Mark an export as expired (now). Returns the row, or None."""
    _exec("update exports set expires_at = now() where id = %s", (export_id,))
    return get_export(export_id)
