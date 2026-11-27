#!/usr/bin/env bash
# One-command setup + start (Mac/Linux). Usage: ./run.sh        (first run also installs deps)
set -e
[ -d venv ] || python3 -m venv venv
source venv/bin/activate
pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
[ -f data/portal.db ] || python ingest.py
uvicorn main:app --port 8000 --workers 1
