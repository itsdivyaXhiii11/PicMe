# Handoff: Person A (backend) -> Person B (downloads + frontend)

Status: **Person A's backend is DONE and tested.** The app runs on Supabase (Postgres + Storage),
sessions live in the database, the admin API works. Person B can start on her part immediately.

---

## 1. The split of `app.py` (already done)

| File | Owner | What is inside |
|---|---|---|
| `main.py` | shared | Setup only: loads the AI model, loads the index from Postgres, creates the FastAPI app, attaches the routers, mounts `static/` |
| `routes_match.py` | **A** | `/api/status`, `/api/match` + `get_session()` / `get_photo()` |
| `routes_download.py` | **B** | `/api/thumb`, `/api/photo`, `/api/zip` — **currently a working placeholder; replace it with your version** |
| `routes_admin.py` | **A** | `/admin/*` endpoints (below) |
| `db.py` | A | Every SQL statement in the project lives here (Postgres, pooled connections) |
| `storage.py` | A | Supabase Storage helper (upload/download/signed URL) |
| `common.py` | A | Drive client + `fetch_bytes()` + settings from `.env` |
| `ingest.py` | A | Indexes photos -> Postgres + Storage (resumable) |
| `schema.sql` | A | The 4 tables |

`app.py` (the old monolith) is removed in this commit — **do not import from it**. The server now starts with:

```powershell
venv\Scripts\python.exe -m uvicorn main:app --port 8000
```

---

## 2. The two functions you use (in `routes_match.py`)

```python
from routes_match import get_session, get_photo

def get_session(token):
    """-> list of photo ids this token may access (sorted by name, then id).
    Raises HTTPException(401, "Session expired. Scan your face again.")
    for a missing OR expired token."""

def get_photo(pid):
    """-> the photo's file name as a string, e.g. 'IMG_0456.jpg'
    Raises HTTPException(404, "No such photo") for an unknown id."""
```

Extra helper already in that file (optional to use):

```python
def allowed(token, pid=None):
    """Session check + photo ownership check.
    401 if the session is dead, 403 "Not your photo" if the photo is not yours.
    Returns the set of the session's photo ids."""
```

**Rules**
- The token comes from the query string: `/api/thumb/<pid>?t=<token>`, `/api/zip?t=<token>`.
- Only the SHA-256 **hash** of a token is stored in the database, so never look up
  sessions yourself with the raw token — always call `get_session(token)`.
- `get_session` returns the photo ids **already sorted by (name, id)** — that is the
  order the grid shows, and it is the order your zip parts must use (B2).

---

## 3. Admin API (for your `static/admin.html`)

Base: `http://<host>:8000`. **Every** endpoint needs the admin password, sent either as a
header `X-Admin-Password: <password>` or as `?password=<password>` (header preferred).

| Endpoint | Request | Success response | Errors |
|---|---|---|---|
| `GET /admin/exports` | header only | `200` `{"count": 2, "exports": [ {...}, {...} ]}` | `401` wrong password · `503` `ADMIN_PASSWORD` not set on the server |
| `POST /admin/exports` | JSON body `{"photo_ids": ["id1","id2"], "part_no": 3, "expires_in": 86400, "id": "optional-id"}` (`expires_in` and `id` optional) | `200` `{"ok": true, "export": {...}}` | `401` |
| `POST /admin/exports/{id}/regenerate` | header only | `200` `{"ok": true, "id": "...", "token": "<raw token>", "url": "/api/zip?t=<token>", "photo_ids": [...], "part_no": 3, "expires_at": "2026-11-13 18:30:00"}` | `401` · `404` `No such export` · `410` `This export is expired` |
| `POST /admin/exports/{id}/expire` | header only | `200` `{"ok": true, "export": {...}}` | `401` · `404` `No such export` |

Each export object looks like this:

```json
{
  "id": "a1b2c3d4e5f6",
  "photo_ids": ["local:p1_einstein.jpg", "1AbCdEfGhIjK"],
  "part_no": 3,
  "created_at": "2026-11-13 12:28:42",
  "expires_at": "2026-11-14 12:28:42"
}
```

(`created_at` / `expires_at` are UTC strings, or `null`.)

**How it works / what to build**

1. Your admin page: password box -> store the password in JS (or ask for it each time)
   -> `GET /admin/exports` -> render a table with one row per export.
2. **Regenerate** button -> `POST /admin/exports/{id}/regenerate` -> show the returned
   `url` as a download link. It is a normal student-style link (`/api/zip?t=<token>`),
   valid for `EXPORT_LINK_TTL` seconds (default 24h). Calling it twice gives two
   different working links — both work at the same time.
3. **Expire** button -> `POST /admin/exports/{id}/expire`. This marks the export expired
   **and kills the link we handed out for it** (the session behind it is deleted), so an
   old link stops working immediately. Regenerating an expired export returns `410`.

Never store a signed URL in the database — the exports table only holds photo ids and a
part number; links are always minted on demand.

---

## 4. Getting the originals and the thumbnails

### Photo ids
- Drive photo: the Google Drive **file id** (`1AbCdEfGhIjKlMnOp...`).
- Local photo: `"local:" + relative path` (e.g. `local:p1_einstein.jpg`).
- The file name of a photo: `get_photo(pid)`.

### Originals (full-resolution)
Use the helpers in `common.py` (do not edit that file, just import):

```python
from common import drive, fetch_bytes

svc = drive()                 # None when no service_account.json -> local folder only
data = fetch_bytes(svc, pid)  # -> bytes of the original (works for Drive ids AND "local:" ids)
```

For your `drive_client.py` (B3): build **one Drive client per thread** (the Drive client is
not thread-safe — the old code used a global `svc_lock`, which is why everything was
serialised), and cap how many downloads run at once with `DRIVE_CONCURRENCY` (default 5).
Retry rate-limit / temporary errors with increasing waits.

### Thumbnails (600 px, quality 80)
Every photo has **two** copies:

| Copy | Where | How to read it |
|---|---|---|
| Local file | `data/thumbs/<pid with '/' and ':' replaced by '_'>.jpg` | `FileResponse(path)` — fastest, no network |
| Supabase Storage (private bucket `thumbs`) | object path `<sha1(pid)[:8]>-<safe_name>.jpg` | `storage.download(path)` -> bytes, or `storage.signed_url(path, seconds)` -> temporary link |

The Storage path of every photo is already in the database — you don't have to build it:
`db.list_photo_ids()` returns `{photo_id: thumb_path}`. (Ask A if you'd rather have a
single helper that returns the thumbnail bytes for a photo id.)

**Your B4 task:** if the local thumbnail file is missing (e.g. the server was redeployed
and lost its disk), do **not** return a 500 — rebuild the thumbnail from the original
(download the original, make a 600 px thumbnail) or serve it from Supabase Storage instead.

---

## 5. Things you must not break

- `routes_download.py` must keep exposing a module-level `router = APIRouter()` —
  `main.py` does `app.include_router(download_router)`.
- Keep importing as `from routes_match import get_session, get_photo` — do not rename them.
- **Do not edit:** `common.py`, `db.py`, `schema.sql`, `ingest.py`, `routes_match.py`,
  `routes_admin.py`. Ask A if you need something added there.
- **You own:** `zipstream.py`, `drive_client.py`, `routes_download.py`, `static/index.html`,
  `static/admin.html`, and the `ZIP_PART_SIZE` / `DRIVE_CONCURRENCY` lines in `.env.example`.

## 6. Settings you add to `.env` (and `.env.example`)

```
ZIP_PART_SIZE=50        # photos per zip part
DRIVE_CONCURRENCY=5     # simultaneous Drive downloads
```

Everything else in `.env` (Supabase keys, `THRESHOLD`, `ADMIN_PASSWORD`, …) is mine and is
documented in `.env.example` — ask A before changing it.

## 7. Current state / how to test

- The database is currently **empty** (0 photos) — the real photos get indexed by
  `python ingest.py` once the Drive folder is connected. The server prints
  `Loaded N photos, M faces` at startup.
- Two ready-made test scripts (start the server first):
  - `venv\Scripts\python.exe test_match_flow.py` — student journey (scan -> photos -> download)
  - `venv\Scripts\python.exe test_admin_api.py` — the admin endpoints above
- `.env` holds the secrets (Supabase URL + service key + DB URL). It is git-ignored and
  must never be committed or sent to the browser.
