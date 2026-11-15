"""Person B tests: B1-B4 (ZIP streaming, parts, Drive client, thumbnails, security).

Run (no server, no Supabase, no Google credentials needed - everything external is mocked):

    python -m pytest test_b_downloads.py -v
"""
import hashlib
import io
import os
import sys
import threading
import time
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import b_mock_backend as mock  # noqa: E402

mock.install()                  # fake routes_match BEFORE importing routes_download

from fastapi import FastAPI                      # noqa: E402
from fastapi.testclient import TestClient        # noqa: E402

import common                                    # noqa: E402
import drive_client                              # noqa: E402
import routes_download                           # noqa: E402
import zipstream                                 # noqa: E402

app = FastAPI()
app.include_router(routes_download.router)
client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    """Every test: 400 fake photos, a fake Drive, no sleeping, empty thumbs dir."""
    ids = mock.make_photos(400)
    mock.SESSIONS.clear()
    mock.add_session("tok400", ids)
    fake = mock.FakeDrive()
    monkeypatch.setattr(drive_client, "_raw_fetch", fake)
    monkeypatch.setattr(drive_client, "_sleep", lambda s: None)
    monkeypatch.setattr(common, "drive", lambda: object())   # a 'client' per thread
    monkeypatch.setattr(routes_download, "THUMBS", str(tmp_path / "thumbs"))
    monkeypatch.setattr(routes_download, "ZIP_PART_SIZE", 50)
    drive_client.reset_stats()
    drive_client._local.__dict__.clear()
    routes_download._paths_cache.update(at=0.0, map={})
    return _Ctx(ids, fake, tmp_path)


class _Ctx:
    def __init__(self, ids, drive, tmp):
        self.ids, self.drive, self.tmp = ids, drive, tmp


def get_zip(token, part):
    r = client.get(f"/api/zip?t={token}&part={part}")
    assert r.status_code == 200, r.text
    return r, zipfile.ZipFile(io.BytesIO(r.content))


# ------------------------------------------------------------------ B1
def test_zip_is_valid_and_numbered(fresh):
    r, z = get_zip("tok400", 1)
    assert z.testzip() is None
    names = z.namelist()
    assert names[0] == "001_IMG_0000.jpg" and names[-1] == "050_IMG_0049.jpg"
    assert len(names) == 50
    assert z.read("001_IMG_0000.jpg") == mock.photo_bytes("drive0000")


def test_no_content_length_and_headers(fresh):
    r = client.get("/api/zip?t=tok400&part=2")
    assert "content-length" not in {k.lower() for k in r.headers}
    assert r.headers["x-zip-parts"] == "8" and r.headers["x-zip-part"] == "2"
    assert "attachment" in r.headers["content-disposition"]


def test_missing_photo_skipped_and_listed(fresh, monkeypatch):
    bad = fresh.ids[3]
    monkeypatch.setattr(drive_client, "_raw_fetch",
                        mock.FakeDrive(fail_forever=[bad], error=mock.make_http_error(404, "notFound")))
    r, z = get_zip("tok400", 1)
    names = z.namelist()
    assert "004_IMG_0003.jpg" not in names and len(names) == 50      # 49 photos + MISSING.txt
    assert names[-1] == "MISSING.txt"
    assert "004_IMG_0003.jpg" in z.read("MISSING.txt").decode()
    assert z.testzip() is None


def test_zip_never_touches_disk(fresh, monkeypatch):
    import tempfile
    def boom(*a, **k):
        raise AssertionError("temp file used")
    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", boom)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", boom)
    get_zip("tok400", 1)


def test_streaming_first_byte_is_fast_and_memory_bounded(fresh, monkeypatch):
    slow = mock.FakeDrive(latency=0.02)
    monkeypatch.setattr(drive_client, "_raw_fetch", slow)
    items = [(p, mock.NAMES[p]) for p in fresh.ids[:50]]
    t0 = time.time()
    gen = zipstream.zip_chunks(items)
    first = next(gen)
    assert time.time() - t0 < 2 and first.startswith(b"PK")
    # prefetch window: only a few photos fetched ahead of what was consumed
    time.sleep(0.2)
    assert slow.calls <= zipstream.ZIP_PREFETCH + 3
    gen.close()


def test_zip64_entries(fresh):
    """Every entry is written in ZIP64 form so archives >4GB are valid."""
    _, z = get_zip("tok400", 1)
    raw = z.fp.getvalue() if hasattr(z.fp, "getvalue") else b""
    # force_zip64: each local header marks both sizes 0xFFFFFFFF (real sizes live in the
    # ZIP64 extra field), so a single photo or the whole archive may exceed 4 GB.
    assert raw[18:26] == b"\xff" * 8
    assert all(i.extract_version >= 45 for i in z.infolist())


# ------------------------------------------------------------------ B2
def test_400_photos_make_8_parts_no_gaps_no_dupes(fresh):
    info = client.get("/api/zip/info?t=tok400").json()
    assert info["total_parts"] == 8 and info["total_photos"] == 400
    assert info["parts"][2] == {"part": 3, "first": 101, "last": 150, "count": 50}
    seen = []
    for p in range(1, 9):
        _, z = get_zip("tok400", p)
        assert len(z.namelist()) == 50
        for n in z.namelist():
            seen.append((n, hashlib.sha256(z.read(n)).hexdigest()))
    assert len({n for n, _ in seen}) == 400                         # no duplicates
    expected = {f"{i + 1:03d}_IMG_{i:04d}.jpg" for i in range(400)}
    assert {n for n, _ in seen} == expected                         # no gaps
    # content matches the right photo
    assert dict(seen)["051_IMG_0050.jpg"] == hashlib.sha256(mock.photo_bytes("drive0050")).hexdigest()


def test_last_partial_part(fresh):
    mock.add_session("tok120", fresh.ids[:120])
    assert client.get("/api/zip/info?t=tok120").json()["total_parts"] == 3
    _, z = get_zip("tok120", 3)
    assert len(z.namelist()) == 20 and z.namelist()[0] == "101_IMG_0100.jpg"


def test_part_validation(fresh):
    assert client.get("/api/zip?t=tok400&part=0").status_code == 400
    assert client.get("/api/zip?t=tok400&part=9").status_code == 400
    assert client.get("/api/zip?t=tok400&part=-3").status_code == 400
    assert client.get("/api/zip?t=tok400&part=abc").status_code == 422
    mock.add_session("empty", [])
    assert client.get("/api/zip?t=empty").status_code == 404


def test_default_part_is_1_and_part_size_env(fresh, monkeypatch):
    monkeypatch.setattr(routes_download, "ZIP_PART_SIZE", 100)
    assert client.get("/api/zip/info?t=tok400").json()["total_parts"] == 4
    _, z = get_zip("tok400", 1)
    assert len(z.namelist()) == 100


def test_order_is_name_then_id(fresh):
    mock.NAMES.update({"zz": "A.jpg", "aa": "A.jpg", "mm": "B.jpg"})
    mock.add_session("ord", ["zz", "mm", "aa"])
    _, z = get_zip("ord", 1)
    assert z.namelist() == ["001_A.jpg", "002_A.jpg", "003_B.jpg"]
    assert z.read("001_A.jpg") == mock.photo_bytes("aa")             # id breaks the tie


# ------------------------------------------------------------------ B3
def test_ten_simultaneous_downloads_succeed_and_respect_limit(fresh, monkeypatch):
    fake = mock.FakeDrive(latency=0.05)
    monkeypatch.setattr(drive_client, "_raw_fetch", fake)
    monkeypatch.setattr(drive_client, "_slots", threading.BoundedSemaphore(5))
    results, errors = [], []

    def work(i):
        try:
            results.append(drive_client.fetch_original(fresh.ids[i]))
        except Exception as e:           # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=work, args=(i,)) for i in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and len(results) == 10
    assert fake.max_in_flight <= 5
    assert drive_client.STATS["max_in_flight"] <= 5


def test_concurrency_limit_is_configurable(fresh, monkeypatch):
    fake = mock.FakeDrive(latency=0.03)
    monkeypatch.setattr(drive_client, "_raw_fetch", fake)
    monkeypatch.setattr(drive_client, "_slots", threading.BoundedSemaphore(2))
    ts = [threading.Thread(target=drive_client.fetch_original, args=(fresh.ids[i],)) for i in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert fake.max_in_flight <= 2


def test_temporary_errors_are_retried(fresh, monkeypatch):
    fake = mock.FakeDrive(fail_first={fresh.ids[0]: 3})               # 429 x3 then OK
    monkeypatch.setattr(drive_client, "_raw_fetch", fake)
    data = drive_client.fetch_original(fresh.ids[0])
    assert data == mock.photo_bytes(fresh.ids[0])
    assert drive_client.STATS["retries"] == 3 and fake.calls == 4


def test_server_errors_and_network_errors_retried(fresh, monkeypatch):
    for err in (mock.make_http_error(503, "backendError"), ConnectionResetError("x"), TimeoutError()):
        fake = mock.FakeDrive(fail_first={fresh.ids[1]: 2}, error=err)
        monkeypatch.setattr(drive_client, "_raw_fetch", fake)
        assert drive_client.fetch_original(fresh.ids[1])


def test_permanent_errors_not_retried(fresh, monkeypatch):
    fake = mock.FakeDrive(fail_forever=[fresh.ids[2]], error=mock.make_http_error(404, "notFound"))
    monkeypatch.setattr(drive_client, "_raw_fetch", fake)
    with pytest.raises(drive_client.FetchError):
        drive_client.fetch_original(fresh.ids[2])
    assert fake.calls == 1


def test_gives_up_after_max_retries(fresh, monkeypatch):
    fake = mock.FakeDrive(fail_forever=[fresh.ids[2]])               # 429 forever
    monkeypatch.setattr(drive_client, "_raw_fetch", fake)
    with pytest.raises(drive_client.FetchError):
        drive_client.fetch_original(fresh.ids[2])
    assert fake.calls == drive_client.MAX_RETRIES + 1


def test_backoff_grows_and_is_capped():
    d = [drive_client.backoff_delay(n) for n in range(1, 12)]
    assert max(d) <= drive_client.BACKOFF_MAX
    assert drive_client.backoff_delay(1) <= drive_client.BACKOFF_BASE
    assert drive_client.backoff_delay(6) > drive_client.backoff_delay(1)


def test_one_client_per_thread(fresh, monkeypatch):
    seen, alive = [], []
    def make_client():
        c = object(); alive.append(c); return c      # keep alive so id() is unique
    monkeypatch.setattr(common, "drive", make_client)
    monkeypatch.setattr(drive_client, "_raw_fetch", lambda svc, pid: seen.append(id(svc)) or b"x")
    def w():
        drive_client.fetch_original("d1"); drive_client.fetch_original("d2")
    ts = [threading.Thread(target=w) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(seen) == 8 and len(set(seen)) == 4                     # 4 threads -> 4 clients


def test_local_photos_still_work(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_client, "_raw_fetch", None)
    monkeypatch.setattr(common, "LOCAL_DIR", str(tmp_path))
    (tmp_path / "p1_einstein.jpg").write_bytes(b"LOCALBYTES")
    assert drive_client.fetch_original("local:p1_einstein.jpg") == b"LOCALBYTES"
    with pytest.raises(drive_client.FetchError):                       # missing: no retry loop
        drive_client.fetch_original("local:nope.jpg")


def test_no_service_account_means_clear_error(fresh, monkeypatch):
    monkeypatch.setattr(drive_client, "_raw_fetch", None)
    monkeypatch.setattr(common, "drive", lambda: None)
    drive_client._local.__dict__.clear()
    with pytest.raises(drive_client.FetchError):
        drive_client.fetch_original("someDriveId")


# ------------------------------------------------------------------ B4
def test_expired_session_is_401_everywhere(fresh):
    mock.SESSIONS["old"] = (time.time() - 5, fresh.ids[:3])
    for url in ("/api/zip?t=old", "/api/zip/info?t=old",
                f"/api/thumb/{fresh.ids[0]}?t=old", f"/api/photo/{fresh.ids[0]}?t=old",
                "/api/zip", "/api/zip?t=garbage"):
        r = client.get(url)
        assert r.status_code == 401, url
        assert "Scan your face again" in r.json()["detail"]


def test_cannot_download_someone_elses_photo(fresh):
    mock.add_session("small", fresh.ids[:3])
    other = fresh.ids[10]
    assert client.get(f"/api/photo/{other}?t=small").status_code == 403
    assert client.get(f"/api/thumb/{other}?t=small").status_code == 403
    assert client.get(f"/api/photo/{fresh.ids[0]}?t=small").status_code == 200
    # a ZIP only contains the session's own photos
    _, z = get_zip("small", 1)
    assert len(z.namelist()) == 3


def test_missing_thumbnail_rebuilt_from_original(fresh, monkeypatch):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (2000, 1500), (200, 30, 30)).save(buf, "JPEG")
    monkeypatch.setattr(drive_client, "_raw_fetch", lambda svc, pid: buf.getvalue())
    monkeypatch.setattr(routes_download.db, "list_photo_ids", lambda: {})
    pid = fresh.ids[0]
    assert not os.path.exists(routes_download.thumb_file(pid))        # thumbs folder is gone
    r = client.get(f"/api/thumb/{pid}?t=tok400")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    img = Image.open(io.BytesIO(r.content))
    assert max(img.size) == 600
    assert os.path.exists(routes_download.thumb_file(pid))            # cached for next time
    assert client.get(f"/api/thumb/{pid}?t=tok400").status_code == 200


def test_missing_thumbnail_prefers_supabase_storage(fresh, monkeypatch):
    pid = fresh.ids[1]
    monkeypatch.setattr(routes_download.db, "list_photo_ids", lambda: {pid: "abc-IMG.jpg"})
    monkeypatch.setattr(routes_download.storage, "download", lambda p: b"STORAGEJPEG" if p == "abc-IMG.jpg" else None)
    monkeypatch.setattr(drive_client, "_raw_fetch", lambda *a: (_ for _ in ()).throw(AssertionError("original used")))
    r = client.get(f"/api/thumb/{pid}?t=tok400")
    assert r.status_code == 200 and r.content == b"STORAGEJPEG"


def test_thumbnail_unrecoverable_is_404_not_500(fresh, monkeypatch):
    monkeypatch.setattr(routes_download.db, "list_photo_ids", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(drive_client, "_raw_fetch", lambda *a: b"not an image")
    r = client.get(f"/api/thumb/{fresh.ids[2]}?t=tok400")
    assert r.status_code == 404
    assert "db down" not in r.text and "Traceback" not in r.text


def test_existing_local_thumbnail_served(fresh):
    pid = fresh.ids[0]
    p = routes_download.thumb_file(pid)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(b"LOCALTHUMB")
    r = client.get(f"/api/thumb/{pid}?t=tok400")
    assert r.status_code == 200 and r.content == b"LOCALTHUMB"


def test_content_disposition_is_escaped():
    evil = 'a"b\r\nSet-Cookie: x=1;/../é.jpg'
    h = routes_download.content_disposition(evil)
    assert "\r" not in h and "\n" not in h
    assert h.count('"') == 2 and "/" not in h.split("filename*=")[0]
    assert "filename*=UTF-8''" in h


def test_single_photo_download_header(fresh):
    mock.NAMES[fresh.ids[0]] = 'we"ird\nname.jpg'
    r = client.get(f"/api/photo/{fresh.ids[0]}?t=tok400")
    assert r.status_code == 200 and r.content == mock.photo_bytes(fresh.ids[0])
    assert "\n" not in r.headers["content-disposition"]


def test_photo_fetch_failure_hides_internal_details(fresh, monkeypatch):
    monkeypatch.setattr(drive_client, "_raw_fetch", mock.FakeDrive(fail_forever=[fresh.ids[0]]))
    r = client.get(f"/api/photo/{fresh.ids[0]}?t=tok400")
    assert r.status_code == 502 and "rateLimit" not in r.text and "drive" not in r.text.lower().replace("photo", "")


def test_zip_filenames_are_sanitised(fresh):
    mock.NAMES[fresh.ids[0]] = "../../etc/passwd"
    mock.add_session("evil", [fresh.ids[0]])
    _, z = get_zip("evil", 1)
    assert z.namelist() == ["001_passwd"]


def test_many_concurrent_zip_requests_respect_drive_limit(fresh, monkeypatch):
    fake = mock.FakeDrive(latency=0.005)
    monkeypatch.setattr(drive_client, "_raw_fetch", fake)
    monkeypatch.setattr(drive_client, "_slots", threading.BoundedSemaphore(5))
    errs = []

    def student():
        try:
            for p in (1, 2):
                r = client.get(f"/api/zip?t=tok400&part={p}")
                assert r.status_code == 200 and zipfile.ZipFile(io.BytesIO(r.content)).testzip() is None
        except Exception as e:            # noqa: BLE001
            errs.append(repr(e))

    ts = [threading.Thread(target=student) for _ in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs, errs
    assert fake.max_in_flight <= 5
