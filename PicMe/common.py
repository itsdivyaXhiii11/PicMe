import io, os, sqlite3
from dotenv import load_dotenv

load_dotenv()
FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "")
KEY_FILE = os.environ.get("SERVICE_ACCOUNT_FILE", "service_account.json")
LOCAL_DIR = os.environ.get("LOCAL_DIR", "photos")
ALLOW_UPLOAD = os.environ.get("ALLOW_SELFIE_UPLOAD", "0") == "1"
DATA = "data"
THUMBS = f"{DATA}/thumbs"
os.makedirs(THUMBS, exist_ok=True)


def db():
    con = sqlite3.connect(f"{DATA}/portal.db")
    con.execute("CREATE TABLE IF NOT EXISTS photos(id TEXT PRIMARY KEY, name TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS faces(photo_id TEXT, emb BLOB)")
    return con


def drive():
    """Returns a Drive client, or None if no service account key is present."""
    if not os.path.exists(KEY_FILE):
        return None
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_file(
        KEY_FILE, scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def fetch_bytes(svc, pid) -> bytes:
    """Photo ids starting with 'local:' come from the photos/ folder, others from Drive."""
    if pid.startswith("local:"):
        with open(os.path.join(LOCAL_DIR, pid[6:]), "rb") as f:
            return f.read()
    from googleapiclient.http import MediaIoBaseDownload
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, svc.files().get_media(fileId=pid, supportsAllDrives=True),
                             chunksize=8 * 1024 * 1024)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()
