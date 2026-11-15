# Person B: delivery + frontend - what was done

## Files
Created: `zipstream.py` (B1), `drive_client.py` (B3), `static/admin.html` (B6),
`loadtest_zip.py` + `loadtest_server.py` (B7), `test_b_downloads.py`, `b_mock_backend.py` (tests/mocks).
Modified (mine only): `routes_download.py` (B2, B4), `static/index.html` (B5), `.env.example` (Person B lines).
Not touched: common.py, db.py, schema.sql, ingest.py, routes_match.py, routes_admin.py.

## New API
- `GET /api/zip?t=TOKEN&part=N`  (part defaults to 1; 400 if out of range) - streamed, no Content-Length
- `GET /api/zip/info?t=TOKEN`    -> `{total_photos, part_size, total_parts, parts:[{part, first, last, count}]}`
- `/api/thumb/{pid}` and `/api/photo/{pid}` keep their URLs.

## Dependencies
None added to requirements.txt. For tests/load test only: `pip install pytest httpx psutil`.

## Install / run
    pip install -r requirements.txt
    cp .env.example .env     # fill in A's values; ZIP_PART_SIZE / DRIVE_CONCURRENCY already listed
    python -m uvicorn main:app --port 8000
Student page: http://localhost:8000/   Admin page: http://localhost:8000/admin.html

## Tests
    python -m pytest test_b_downloads.py -v                       # 32 tests, all mocked
    python loadtest_server.py --port 8765 &                       # mock server
    python loadtest_zip.py --base http://127.0.0.1:8765 --token loadtest --students 50 --server-pid <pid>

## Test status (honest)
PASSED here: py_compile on all files; 32/32 `test_b_downloads.py`; JS syntax check of index.html + admin.html;
mock load test 50 students x 8 parts (400 photos, 5% injected 429s, DRIVE_CONCURRENCY=5): 50/50 OK,
content verified, max Drive concurrency 5, peak server RSS ~119 MB, 987 retries recovered.
COULD NOT RUN here: `test_match_flow.py` and `test_admin_api.py` (need a running server with real Supabase
credentials + AI model + .env ADMIN_PASSWORD); real Google Drive; real event photos; real browsers
(the two HTML pages were only syntax-checked, not clicked through); load test against the real server.
The "50 students, no failed zips" criterion is proven ONLY for the mock environment.

## Needs a human
1. Run test_match_flow.py / test_admin_api.py on your machine (server started), then the load test with a real token.
2. Open both pages in a browser: scan -> 48 thumbnails -> Show more -> Part buttons; admin login/regenerate/expire.
3. Person A (optional, small): `main.py` still defines `svc`/`svc_lock`; nothing uses them now - delete when convenient.
4. Real Drive tuning: raise/lower DRIVE_CONCURRENCY if Google returns many 429s (watch `retries` in the logs).
5. Admin regenerate links are student-style tokens in the URL (A's design); the admin password is never in a URL.

## Push to GitHub
    git checkout -b person-b-delivery
    git add zipstream.py drive_client.py routes_download.py static/index.html static/admin.html \
            .env.example loadtest_zip.py loadtest_server.py test_b_downloads.py b_mock_backend.py PERSON_B_NOTES.md
    git commit -m "Person B: streaming zip parts, drive client, thumb recovery, gallery, admin page, load test"
    git push -u origin person-b-delivery     # then open a pull request
(Never `git add .env` or service_account.json.)
