"""Quick check: can the robot account see the Drive folder?

    venv\\Scripts\\python.exe test_drive_access.py

Prints every photo id + name it can see in DRIVE_FOLDER_ID.
If this fails, the folder is not shared with the service account email yet
(the error message says exactly what is wrong).
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.getcwd(), ".env"))

sys.path.insert(0, os.getcwd())
from common import FOLDER_ID, KEY_FILE, drive  # noqa: E402

print("SERVICE_ACCOUNT_FILE:", KEY_FILE, "(exists)" if os.path.exists(KEY_FILE) else "(MISSING!)")
print("DRIVE_FOLDER_ID     :", FOLDER_ID or "(MISSING - set it in .env)")

if not os.path.exists(KEY_FILE):
    sys.exit("The key file is not where .env says it is.")
if not FOLDER_ID:
    sys.exit("DRIVE_FOLDER_ID is empty in .env")

svc = drive()
if svc is None:
    sys.exit("drive() returned None - no service account key found.")

try:
    r = svc.files().list(
        q=f"'{FOLDER_ID}' in parents and trashed=false",
        fields="nextPageToken, files(id,name,mimeType)", pageSize=100,
        supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
except Exception as e:
    print("\nFAILED to read the folder:")
    print(" ", str(e)[:600])
    print("\nMost likely cause: the folder is not shared with")
    print("  picme-ingest@picme-drive-514605.iam.gserviceaccount.com")
    sys.exit(1)

files = [f for f in r.get("files", []) if f["mimeType"] in ("image/jpeg", "image/png")]
print(f"\nACCESS OK - {len(r.get('files', []))} file(s) in the folder, {len(files)} are photos:")
for f in files:
    print(f"  {f['id']}   {f['name']}")
if not files:
    print("  (no photos found - upload some .jpg/.png into the folder first)")
