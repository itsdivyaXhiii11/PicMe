# PicMe

Face-search photo portal for graduation day (and any event).

Students scan their face and see/download only the photos they appear in.

## Quick test (no Google Drive needed)
1. Put some JPG/PNG photos with faces into `photos/`
2. `./run.sh`  (creates venv, installs, indexes photos, starts server)
3. Open http://localhost:8000 and scan

## Use your Google Drive photos
1. Google Cloud: create a project, enable Drive API, create a Service Account, download its JSON key as `service_account.json` here
2. Share the Drive folder with the service account's `client_email` (Viewer) and turn off "anyone with the link"
3. In `.env` set `DRIVE_FOLDER_ID` (the part after `/folders/` in the URL)
4. `python ingest.py` (slow, resumable) then `uvicorn app:app --port 8000`

## Phone / real event
Phone cameras need HTTPS. Test with `cloudflared tunnel --url http://localhost:8000`.
For the event, deploy on a VPS/Render with HTTPS, set `ALLOW_SELFIE_UPLOAD=0`.

## Tuning
`THRESHOLD` in app.py (0.45): higher = stricter, lower = more matches.

## Privacy
Selfies are never saved. Sessions last 1 hour. Delete `data/` after the event.
Group photos contain other faces: get college consent (India DPDP Act).
