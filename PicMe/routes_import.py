import json, os, re, subprocess, sys, threading, uuid
from datetime import date, datetime
from pathlib import Path
from fastapi import APIRouter, Form, Header, HTTPException
from fastapi.responses import FileResponse

router = APIRouter()
BASE = Path(__file__).parent
JOBS = {}
IMG = ("image/jpeg", "image/png", "image/webp")


def _auth(token):
    need = os.getenv("ADMIN_TOKEN")
    if need and token != need:
        raise HTTPException(401, "Wrong or missing admin token")


def folder_id(link):
    link = link.strip()
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", link) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", link)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", link):
        return link
    raise HTTPException(400, "That doesn't look like a Google Drive folder link")


def drive_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    path = BASE / "service_account.json"
    if not path.exists():
        raise HTTPException(500, "service_account.json is missing from the project folder")
    creds = service_account.Credentials.from_service_account_file(
        str(path), scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=creds, cache_discovery=False), creds.service_account_email


def count_images(svc, fid):
    total, token = 0, None
    while True:
        r = svc.files().list(
            q=f"'{fid}' in parents and trashed=false",
            fields="nextPageToken, files(id,mimeType)", pageSize=1000, pageToken=token,
            supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        for f in r.get("files", []):
            if f["mimeType"] in IMG:
                total += 1
        token = r.get("nextPageToken")
        if not token:
            return total


def save_event(ev):
    p = BASE / "data" / "events.json"
    p.parent.mkdir(exist_ok=True)
    items = json.loads(p.read_text()) if p.exists() else []
    items.append(ev)
    p.write_text(json.dumps(items, indent=2))


def run_ingest(job_id, ev):
    job = JOBS[job_id]
    env = dict(os.environ, DRIVE_FOLDER_ID=ev["folder_id"], EVENT_ID=ev["id"],
               EVENT_NAME=ev["name"], EVENT_DATE=ev["date"])
    try:
        p = subprocess.Popen([sys.executable, "ingest.py"], cwd=str(BASE), env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in p.stdout:
            job["log"] = (job["log"] + [line.rstrip()])[-30:]
        p.wait()
        job["status"] = "done" if p.returncode == 0 else "failed"
    except Exception as e:
        job["status"] = "failed"
        job["log"].append(str(e))


@router.get("/admin/import")
def import_page():
    return FileResponse(BASE / "static" / "admin_import.html")


@router.post("/admin/import-drive")
def import_drive(event_name: str = Form(...), event_date: str = Form(...),
                 drive_link: str = Form(...), x_admin_token: str = Header(None)):
    _auth(x_admin_token)
    name = event_name.strip()
    if not name:
        raise HTTPException(400, "Event name is required")
    try:
        datetime.strptime(event_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Event date must be a valid date")
    fid = folder_id(drive_link)
    svc, email = drive_service()
    try:
        n = count_images(svc, fid)
    except Exception:
        raise HTTPException(400, f"Can't read that folder. Share it (Viewer) with {email} and try again.")
    if n == 0:
        raise HTTPException(400, "No JPG, PNG or WebP photos found directly in that folder")
    ev = {"id": uuid.uuid4().hex[:8], "name": name, "date": event_date,
          "folder_id": fid, "photos": n, "created": date.today().isoformat()}
    save_event(ev)
    job_id = ev["id"]
    JOBS[job_id] = {"status": "running", "event": ev, "log": []}
    threading.Thread(target=run_ingest, args=(job_id, ev), daemon=True).start()
    return {"job_id": job_id, "photos": n, "event": ev}


@router.get("/admin/import-status/{job_id}")
def import_status(job_id: str, x_admin_token: str = Header(None)):
    _auth(x_admin_token)
    if job_id not in JOBS:
        raise HTTPException(404, "Unknown job")
    return JOBS[job_id]


# ---------- event management ----------
import json
from pathlib import Path
from fastapi import UploadFile, File

EVENTS_FILE = Path("events.json")
CRED_FILE = Path("service_account.json")


def _load_events():
    try:
        return json.loads(EVENTS_FILE.read_text())
    except Exception:
        return []


@router.get("/admin/events")
def list_events(x_admin_token: str = Header(None)):
    _auth(x_admin_token)
    return _load_events()


@router.delete("/admin/events/{event_id}")
def delete_event(event_id: str, x_admin_token: str = Header(None)):
    _auth(x_admin_token)
    evs = _load_events()
    keep = [e for e in evs if e["id"] != event_id]
    if len(keep) == len(evs):
        raise HTTPException(404, "Unknown event")
    EVENTS_FILE.write_text(json.dumps(keep, indent=2))
    JOBS.pop(event_id, None)
    return {"deleted": event_id}


@router.post("/admin/events/{event_id}/resync")
def resync_event(event_id: str, x_admin_token: str = Header(None)):
    _auth(x_admin_token)
    ev = next((e for e in _load_events() if e["id"] == event_id), None)
    if not ev:
        raise HTTPException(404, "Unknown event")
    if JOBS.get(event_id, {}).get("status") == "running":
        raise HTTPException(409, "Already syncing")
    JOBS[event_id] = {"status": "running", "event": ev, "log": []}
    threading.Thread(target=run_ingest, args=(event_id, ev), daemon=True).start()
    return {"job_id": event_id}


@router.post("/admin/upload-credentials")
async def upload_credentials(file: UploadFile = File(...), x_admin_token: str = Header(None)):
    _auth(x_admin_token)
    if not file.filename.lower().endswith(".json"):
        raise HTTPException(400, "Upload a .json file")
    try:
        data = json.loads((await file.read()).decode())
    except Exception:
        raise HTTPException(400, "Not valid JSON")
    if data.get("type") != "service_account" or "client_email" not in data:
        raise HTTPException(400, "This isn't a Google service account key")
    CRED_FILE.write_text(json.dumps(data))
    CRED_FILE.chmod(0o600)
    return {"ok": True, "email": data["client_email"]}


@router.get("/admin/credentials-info")
def credentials_info(x_admin_token: str = Header(None)):
    _auth(x_admin_token)
    try:
        return {"email": json.loads(CRED_FILE.read_text())["client_email"]}
    except Exception:
        return {"email": None}
