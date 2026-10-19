# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - model download jobs against a LOCAL stand-in server (MH-02).

Endpoints: ``POST /api/model-catalog/download``, ``GET /download-jobs``, ``GET /download/{id}/progress`` (SSE),
``WS /download/{id}/ws``, ``DELETE /download/{id}`` (cancel) and ``POST /verify``.

How it runs without the internet: a ``http.server`` on 127.0.0.1 serves the payloads; the model version's ``manifest_url`` is
seeded straight into the throw-away database (the HTTP API has no "add version" call - same approach as ``test_model_hub.py``);
the repo's own ``data/bin/aria2c/aria2c.exe`` does the transfer, put first on the backend's ``PATH`` (the backend finds
``aria2c`` only through ``PATH``; without it ``POST /download`` is a clean 503 ``aria2c.start_failed``, which is tested in
``test_downloads_no_aria2c.py`` - a separate module, because this one keeps a backend alive on port 8989 for its whole run).

NOT covered (deliberately):

* **Real model files / the real catalog URL / the release manifest.** Everything is a few KB from loopback.
* **Resume of a partly downloaded file after a restart, multi-connection splitting, bandwidth limits, proxies.**
* **The WebSocket variant's frame sequence for a LIVE job** beyond the not-found case - it duplicates the SSE path.
* **Concurrent jobs beyond two at once; the aria2c daemon crash / restart path.**

Isolation (verified): the backend runs with ``cwd`` = a temp folder, because aria2c writes ``models/<name>`` relative to the
backend's working directory - an earlier exploration run with the repo as ``cwd`` created two files in the repo's real
``models/`` folder and had to delete them by hand. This module asserts that the repo's ``models/`` listing is unchanged and
that no ``aria2c`` process outlives the backend. Port 6810 (the aria2c RPC daemon) must be free at the start, otherwise the
module skips instead of fighting another process for it.

Observed behaviour worth pinning:

* The file lands in ``<backend cwd>/models/<target_filename>``; ``verify`` and the blob store read
  ``<data dir>/blobs/<category>/<name>``. A download is therefore NOT found by ``verify`` unless something moves it
  (MH-ISS-01).
* A job is ``running`` until a client reads its progress stream: the stream is what finalises the job. Without a reader it
  stays in ``download-jobs`` as ``running`` even though the file is complete (MH-ISS-02).
* Invalid ``target_filename`` / ``storage_*`` values that the domain rejects (``..``, separators, upper-case category) come
  back as 500 instead of 400 (MH-ISS-03). No file is written outside the intended folder.
* Cancelling leaves the partial file and its ``.aria2`` control file behind (MH-ISS-04).
"""

from __future__ import annotations

import hashlib
import http.server
import itertools
import json
import shutil
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import psutil
import pytest
from websockets.sync.client import connect

from tests.e2e import conftest as c
from tests.e2e._owned_backend import OwnedBackend, owned_backend
from tests.e2e.conftest import REPO_ROOT

pytestmark = pytest.mark.e2e

M = "/api/model-catalog"
ARIA_DIR = REPO_ROOT / "data" / "bin" / "aria2c"
FAST = b"E2E-MODEL-BYTES" * 1000          # 15 000 bytes
SLOW = b"S" * 400_000                      # ~25 s at the throttle below
SHA_FAST = hashlib.sha256(FAST).hexdigest()
RPC_PORT = 6810
_SEQ = itertools.count(1)


# --------------------------------------------------------------------------
# the loopback stand-in server
# --------------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []
    slow_bytes_sent = 0  # counted at the SERVER: the file size on disk lags because aria2c caches writes

    def do_GET(self) -> None:  # noqa: N802
        _Handler.hits.append(self.path)
        if self.path.startswith("/slow"):
            self.send_response(200)
            self.send_header("Content-Length", str(len(SLOW)))
            self.end_headers()
            try:
                for i in range(0, len(SLOW), 4000):
                    self.wfile.write(SLOW[i:i + 4000])
                    self.wfile.flush()
                    _Handler.slow_bytes_sent += 4000
                    time.sleep(0.25)
            except Exception:  # noqa: BLE001 - the client cancelled
                pass
        elif self.path.startswith("/missing"):
            self.send_response(404)
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(len(FAST)))
            self.end_headers()
            self.wfile.write(FAST)

    def log_message(self, *a: object) -> None:
        pass


@pytest.fixture(scope="module")
def stand_in() -> Iterator[str]:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


# --------------------------------------------------------------------------
# one backend for the module, with an isolated cwd and aria2c on PATH
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def models_untouched() -> Iterator[None]:
    root = REPO_ROOT / "models"
    before = sorted(p.name for p in root.iterdir()) if root.exists() else None
    yield
    after = sorted(p.name for p in root.iterdir()) if root.exists() else None
    assert after == before, f"the repo's models/ folder changed: {before} -> {after}"


@pytest.fixture(scope="module")
def dl(models_untouched: None) -> Iterator[tuple[OwnedBackend, httpx.Client, Path]]:
    if not (ARIA_DIR / "aria2c.exe").is_file():
        pytest.skip("data/bin/aria2c/aria2c.exe is not installed")
    if not c._port_is_free(c.E2E_HOST, RPC_PORT):
        pytest.skip(f"port {RPC_PORT} (aria2c RPC) is in use by another process")
    aria_before = {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower().startswith("aria2c")}
    cwd = Path(tempfile.mkdtemp(prefix="qai-e2e-cwd-"))
    try:
        with owned_backend() as ob:
            cl = ob.start(cwd=cwd, path_prepend=ARIA_DIR)
            yield ob, cl, cwd
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            left = [p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower().startswith("aria2c")
                    and p.pid not in aria_before]
            if not left:
                break
            time.sleep(0.3)
        assert not left, f"aria2c process(es) outlived the backend: {left}"
    finally:
        shutil.rmtree(cwd, ignore_errors=True)


@pytest.fixture
def seed(dl: tuple) -> Callable[..., tuple[str, str]]:
    """Register an entry and write its version row (with the stand-in URL) into the throw-away database."""
    ob, cl, _cwd = dl

    def _seed(name: str, path: str, *, sha: str = SHA_FAST, size: int = len(FAST)) -> tuple[str, str]:
        # The backend is shared by the whole module, so a name may be seeded by several parametrised cases.
        n = next(_SEQ)
        mid, vid = f"e2e-{name}-{n}", f"v-{name}-{n}"
        r = cl.post(f"{M}/entries", json={"model_id": mid, "name": mid, "provider": "local", "source_url": "https://example.invalid/x",
                                          "description": "d", "tags": []})
        assert r.status_code == 201, r.text
        con = sqlite3.connect(str(ob.data_dir / "db" / "qai.db"), timeout=15)
        try:
            con.execute("INSERT INTO model_catalog_version (id,parent_model_id,checksum_algorithm,checksum_value,size_bytes,manifest_url,status)"
                        " VALUES (?,?,?,?,?,?,?)", (vid, mid, "sha256", sha, size, path, "published"))
            con.commit()
        finally:
            con.close()
        return mid, vid

    return _seed


def _start(cl: httpx.Client, mid: str, vid: str, filename: str) -> httpx.Response:
    return cl.post(f"{M}/download", json={"model_id": mid, "version_id": vid, "target_filename": filename})


def _frames(cl: httpx.Client, job_id: str, *, until: tuple[str, ...] = ("done", "error"), seconds: float = 20.0) -> list[tuple[str, dict]]:
    """Read the progress SSE until a terminal frame; returns [(event, data)]."""
    out: list[tuple[str, dict]] = []
    buf = ""
    end = time.monotonic() + seconds
    with cl.stream("GET", f"{M}/download/{job_id}/progress", timeout=httpx.Timeout(10.0, read=seconds)) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        for chunk in r.iter_text():
            buf += chunk
            while "\n\n" in buf:
                frame, buf = buf.split("\n\n", 1)
                fields = dict(l.split(": ", 1) for l in frame.splitlines() if ": " in l)
                if "event" in fields:
                    out.append((fields["event"], json.loads(fields.get("data", "{}"))))
            if out and out[-1][0] in until or time.monotonic() > end:
                break
    return out


def _job(cl: httpx.Client, job_id: str) -> dict | None:
    return next((j for j in cl.get(f"{M}/download-jobs").json()["jobs"] if j["job_id"] == job_id), None)


# --------------------------------------------------------------------------
# starting a download
# --------------------------------------------------------------------------


def test_start_returns_202_with_a_running_job(dl: tuple, seed: Callable, stand_in: str) -> None:
    _ob, cl, _cwd = dl
    mid, vid = seed("start", f"{stand_in}/fast.bin")
    r = _start(cl, mid, vid, "start.bin")
    assert r.status_code == 202
    j = r.json()
    assert set(j) == {"job_id", "target_model_version_id", "state", "bytes_downloaded", "total_bytes", "speed_bps", "eta_seconds", "failure_reason"}
    assert (j["target_model_version_id"], j["state"], j["bytes_downloaded"], j["failure_reason"]) == (vid, "running", 0, None)
    assert len(j["job_id"]) == 26
    _frames(cl, j["job_id"])  # let it finish so the next test starts clean


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"model_id": "nope", "version_id": "v", "target_filename": "x.bin"}, 404, "model_catalog.entry_not_found"),
        ({"model_id": "{mid}", "version_id": "nope", "target_filename": "x.bin"}, 404, "model_catalog.version_not_found"),
        ({"model_id": "{mid}", "version_id": "{vid}", "target_filename": ""}, 400, "request.validation"),
        ({"model_id": "{mid}", "version_id": "{vid}"}, 400, "request.validation"),
        ({"model_id": "{mid}", "version_id": "{vid}", "target_filename": "x" * 256}, 400, "request.validation"),
        ({}, 400, "request.validation"),
    ],
)
def test_start_validation(dl: tuple, seed: Callable, stand_in: str, body: dict, status: int, code: str) -> None:
    _ob, cl, _cwd = dl
    mid, vid = seed("val", f"{stand_in}/fast.bin") if "{mid}" in json.dumps(body) else ("", "")
    body = {k: v.replace("{mid}", mid).replace("{vid}", vid) for k, v in body.items()}
    r = cl.post(f"{M}/download", json=body)
    assert r.status_code == status and r.json()["code"] == code
    assert not any(j["target_model_version_id"] == vid for j in cl.get(f"{M}/download-jobs").json()["jobs"] if vid)


@pytest.mark.parametrize("name", ["../x.bin", "a/b.bin", "a\\b.bin", "x..y.bin"])
def test_a_filename_the_domain_rejects_is_a_500_not_a_400(dl: tuple, seed: Callable, stand_in: str, name: str) -> None:
    """KNOWN GAP (current behaviour, pinned) - MH-ISS-03: ``StorageKey`` raises ``ValueError`` for separators and ``..``;
    nothing maps it, so the client sees ``internal.unexpected``. The path guard itself works: nothing is written outside the
    working folder. BUT the job row is created BEFORE the key is validated, so every rejected request leaves one ``queued``
    job behind that never runs and is never cleaned up (MH-ISS-05)."""
    _ob, cl, cwd = dl
    mid, vid = seed("badname", f"{stand_in}/fast.bin")
    before = sorted(p.name for p in cwd.rglob("*"))
    queued_before = [j for j in cl.get(f"{M}/download-jobs").json()["jobs"] if j["target_model_version_id"] == vid]
    assert queued_before == []
    r = _start(cl, mid, vid, name)
    assert r.status_code == 500 and r.json()["code"] == "internal.unexpected"
    assert sorted(p.name for p in cwd.rglob("*")) == before
    leaked = [j for j in cl.get(f"{M}/download-jobs").json()["jobs"] if j["target_model_version_id"] == vid]
    assert [(j["state"], j["bytes_downloaded"], j["failure_reason"]) for j in leaked] == [("queued", 0, None)]
    cl.delete(f"{M}/download/{leaked[0]['job_id']}")  # best effort; a queued job may or may not be cancellable


# --------------------------------------------------------------------------
# progress, completion, and where the file lands
# --------------------------------------------------------------------------


def test_progress_stream_reports_completion_and_the_file_lands_in_the_backend_cwd(dl: tuple, seed: Callable, stand_in: str) -> None:
    ob, cl, cwd = dl
    mid, vid = seed("happy", f"{stand_in}/fast.bin")
    jid = _start(cl, mid, vid, "happy.bin").json()["job_id"]
    frames = _frames(cl, jid)
    assert frames[-1] == ("done", {})
    progress = [d for n, d in frames if n == "progress"]
    last = progress[-1]
    assert last == {"bytes_downloaded": len(FAST), "total_bytes": len(FAST), "speed_bps": 0.0, "eta_seconds": 0.0, "percent": 100.0, "is_complete": True}
    assert all(set(p) == set(last) for p in progress)
    assert [p["bytes_downloaded"] for p in progress] == sorted(p["bytes_downloaded"] for p in progress), "monotonic"
    landed = cwd / "models" / "happy.bin"
    assert landed.read_bytes() == FAST
    assert not (ob.data_dir / "blobs" / "models" / "happy.bin").exists(), "NOT where verify looks - see MH-ISS-01"
    assert not list((cwd / "models").glob("happy.bin.aria2")), "no control file once complete"


def test_a_finished_job_is_terminal_and_can_no_longer_be_cancelled(dl: tuple, seed: Callable, stand_in: str) -> None:
    _ob, cl, _cwd = dl
    mid, vid = seed("term", f"{stand_in}/fast.bin")
    jid = _start(cl, mid, vid, "term.bin").json()["job_id"]
    _frames(cl, jid)
    assert _job(cl, jid) is None, "completed jobs leave the active list"
    r = cl.delete(f"{M}/download/{jid}")
    assert r.status_code == 412 and r.json()["code"] == "model_catalog.download_job_terminated"
    assert "state='completed'" in r.json()["message"]


def test_a_job_stays_running_until_someone_reads_its_progress(dl: tuple, seed: Callable, stand_in: str) -> None:
    """KNOWN GAP (current behaviour, pinned) - MH-ISS-02: the file is complete after a moment, but ``download-jobs`` keeps
    reporting ``running`` with 0 bytes until a client opens the progress stream, which is what finalises the job."""
    _ob, cl, cwd = dl
    mid, vid = seed("unread", f"{stand_in}/fast.bin")
    jid = _start(cl, mid, vid, "unread.bin").json()["job_id"]
    deadline = time.monotonic() + 15
    while not (cwd / "models" / "unread.bin").exists() and time.monotonic() < deadline:
        time.sleep(0.3)
    assert (cwd / "models" / "unread.bin").read_bytes() == FAST, "the transfer itself finished"
    time.sleep(1.5)
    j = _job(cl, jid)
    assert j is not None and j["state"] == "running" and j["bytes_downloaded"] == 0 and j["total_bytes"] is None
    _frames(cl, jid)
    assert _job(cl, jid) is None, "reading the stream finalises it"


def test_progress_stream_headers(dl: tuple, seed: Callable, stand_in: str) -> None:
    _ob, cl, _cwd = dl
    mid, vid = seed("hdr", f"{stand_in}/fast.bin")
    jid = _start(cl, mid, vid, "hdr.bin").json()["job_id"]
    with cl.stream("GET", f"{M}/download/{jid}/progress", timeout=httpx.Timeout(10.0, read=20.0)) as r:
        assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"
        for _ in r.iter_text():
            pass


def test_progress_stream_of_an_unknown_job_is_a_404_envelope(dl: tuple) -> None:
    _ob, cl, _cwd = dl
    r = cl.get(f"{M}/download/NOPE/progress")
    assert r.status_code == 404 and r.json()["code"] == "model_catalog.download_job_not_found"
    assert r.json()["details"] == {"resource_type": "download_job", "resource_id": "NOPE"}


def test_a_source_that_answers_404_ends_the_stream_with_an_error_frame(dl: tuple, seed: Callable, stand_in: str) -> None:
    _ob, cl, cwd = dl
    mid, vid = seed("gone", f"{stand_in}/missing.bin")
    jid = _start(cl, mid, vid, "gone.bin").json()["job_id"]
    frames = _frames(cl, jid)
    assert [n for n, _ in frames] == ["error"]
    err = frames[0][1]
    assert err["code"] == "model_catalog.aria2c_rpc.aria_error" and err["message"] == "aria2c reported error: Resource not found"
    assert err["details"] == {"service": "aria2c_rpc"}
    assert not (cwd / "models" / "gone.bin").exists()


def test_the_same_target_name_twice_is_accepted(dl: tuple, seed: Callable, stand_in: str) -> None:
    _ob, cl, cwd = dl
    mid, vid = seed("twice", f"{stand_in}/fast.bin")
    j1 = _start(cl, mid, vid, "twice.bin").json()["job_id"]
    _frames(cl, j1)
    r = _start(cl, mid, vid, "twice.bin")
    assert r.status_code == 202 and r.json()["job_id"] != j1
    _frames(cl, r.json()["job_id"])
    assert (cwd / "models" / "twice.bin").read_bytes() == FAST


# --------------------------------------------------------------------------
# cancel
# --------------------------------------------------------------------------


def test_cancel_a_running_download(dl: tuple, seed: Callable, stand_in: str) -> None:
    _ob, cl, cwd = dl
    mid, vid = seed("slow", f"{stand_in}/slow.bin", sha="0" * 64, size=len(SLOW))
    jid = _start(cl, mid, vid, "slow.bin").json()["job_id"]
    deadline = time.monotonic() + 15
    while not (cwd / "models" / "slow.bin").exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    time.sleep(1.5)
    r = cl.delete(f"{M}/download/{jid}")
    assert r.status_code == 204 and r.content == b""
    assert _job(cl, jid) is None, "a cancelled job leaves the active list"
    again = cl.delete(f"{M}/download/{jid}")
    assert again.status_code == 412 and "state='cancelled'" in again.json()["message"]
    time.sleep(1.0)  # let the connection teardown settle
    sent = _Handler.slow_bytes_sent
    time.sleep(2.0)  # the server would have written ~32 KB more if aria2c were still reading
    assert _Handler.slow_bytes_sent - sent <= 4000, "the transfer really stopped: the server sees no more reads after the cancel"
    assert _Handler.slow_bytes_sent < len(SLOW)


def test_cancel_leaves_the_partial_file_and_its_control_file(dl: tuple, seed: Callable, stand_in: str) -> None:
    """KNOWN GAP (current behaviour, pinned) - MH-ISS-04: nothing removes ``slow2.bin`` / ``slow2.bin.aria2`` after a cancel."""
    _ob, cl, cwd = dl
    mid, vid = seed("slow2", f"{stand_in}/slow.bin", sha="0" * 64, size=len(SLOW))
    jid = _start(cl, mid, vid, "slow2.bin").json()["job_id"]
    deadline = time.monotonic() + 15
    while not (cwd / "models" / "slow2.bin").exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    time.sleep(1.5)
    assert cl.delete(f"{M}/download/{jid}").status_code == 204
    time.sleep(1.0)
    names = {p.name for p in (cwd / "models").iterdir()}
    assert {"slow2.bin", "slow2.bin.aria2"} <= names


def test_cancel_unknown_job_is_404(dl: tuple) -> None:
    _ob, cl, _cwd = dl
    r = cl.delete(f"{M}/download/NOPE")
    assert r.status_code == 404 and r.json()["code"] == "model_catalog.download_job_not_found"


# --------------------------------------------------------------------------
# download-jobs list
# --------------------------------------------------------------------------


def test_the_list_only_holds_active_jobs_and_has_a_fixed_shape(dl: tuple, seed: Callable, stand_in: str) -> None:
    _ob, cl, _cwd = dl
    mid, vid = seed("lst", f"{stand_in}/slow.bin", sha="0" * 64, size=len(SLOW))
    jid = _start(cl, mid, vid, "lst.bin").json()["job_id"]
    try:
        body = cl.get(f"{M}/download-jobs").json()
        assert list(body) == ["jobs"]
        mine = next(j for j in body["jobs"] if j["job_id"] == jid)
        assert set(mine) == {"job_id", "target_model_version_id", "state", "bytes_downloaded", "total_bytes", "speed_bps", "eta_seconds", "failure_reason"}
        assert mine["state"] == "running"
    finally:
        cl.delete(f"{M}/download/{jid}")
    assert _job(cl, jid) is None


# --------------------------------------------------------------------------
# WebSocket variant (not-found path only)
# --------------------------------------------------------------------------


def test_ws_progress_of_an_unknown_job_sends_an_error_frame_and_closes_4404(dl: tuple) -> None:
    with connect(f"ws://{c.E2E_HOST}:{c.E2E_PORT}{M}/download/NOPE/ws", open_timeout=5) as ws:
        frame = json.loads(ws.recv(timeout=5))
        assert frame["type"] == "error" and frame["code"] == "not_found" and "NOPE" in frame["message"]
        with pytest.raises(Exception) as exc:
            ws.recv(timeout=5)
        assert "4404" in str(exc.value)


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


def _verify(cl: httpx.Client, mid: str, vid: str, category: str = "models", name: str = "v.bin") -> httpx.Response:
    return cl.post(f"{M}/verify", json={"model_id": mid, "version_id": vid, "storage_category": category, "storage_name": name})


def test_a_downloaded_file_is_not_found_by_verify_until_it_is_in_the_blob_store(dl: tuple, seed: Callable, stand_in: str) -> None:
    """KNOWN GAP (current behaviour, pinned) - MH-ISS-01: ``verify`` reads ``<data>/blobs/models/<name>``; the downloader wrote
    ``<cwd>/models/<name>``. The missing blob is an unmapped ``FileNotFoundError`` (500) rather than a 404."""
    ob, cl, cwd = dl
    mid, vid = seed("ver0", f"{stand_in}/fast.bin")
    _frames(cl, _start(cl, mid, vid, "ver0.bin").json()["job_id"])
    assert (cwd / "models" / "ver0.bin").read_bytes() == FAST
    r = _verify(cl, mid, vid, name="ver0.bin")
    assert r.status_code == 500 and r.json()["code"] == "internal.unexpected"
    assert "blob not found at" in ob.log_text() and "blobs" in ob.log_text()


def test_verify_accepts_a_matching_file_and_rejects_a_changed_one(dl: tuple, seed: Callable, stand_in: str) -> None:
    ob, cl, _cwd = dl
    mid, vid = seed("ver1", f"{stand_in}/fast.bin")
    blob = ob.data_dir / "blobs" / "models" / "ver1.bin"
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(FAST)
    r = _verify(cl, mid, vid, name="ver1.bin")
    assert r.status_code == 200 and r.json() == {"status": "verified"}
    blob.write_bytes(FAST + b"x")
    r = _verify(cl, mid, vid, name="ver1.bin")
    assert r.status_code == 422 and r.json()["code"] == "model_catalog.checksum_mismatch"
    d = r.json()["details"]
    assert d["expected"] == SHA_FAST and d["actual"] == hashlib.sha256(FAST + b"x").hexdigest() and d["version_id"] == vid


def test_verify_of_an_empty_file_is_a_mismatch(dl: tuple, seed: Callable, stand_in: str) -> None:
    ob, cl, _cwd = dl
    mid, vid = seed("ver2", f"{stand_in}/fast.bin")
    blob = ob.data_dir / "blobs" / "models" / "ver2.bin"
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"")
    r = _verify(cl, mid, vid, name="ver2.bin")
    assert r.status_code == 422 and r.json()["details"]["actual"] == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize(
    ("which", "status", "code"),
    [("model", 404, "model_catalog.entry_not_found"), ("version", 404, "model_catalog.version_not_found")],
)
def test_verify_unknown_model_or_version(dl: tuple, seed: Callable, stand_in: str, which: str, status: int, code: str) -> None:
    _ob, cl, _cwd = dl
    mid, vid = seed("ver3", f"{stand_in}/fast.bin")
    r = _verify(cl, "nope" if which == "model" else mid, "nope" if which == "version" else vid)
    assert r.status_code == status and r.json()["code"] == code


@pytest.mark.parametrize(
    ("category", "name"),
    [("models", "../v.bin"), ("models", "a/b.bin"), ("..", "v.bin"), ("Models", "v.bin"), ("with space", "v.bin")],
)
def test_verify_with_a_storage_key_the_domain_rejects_is_a_500_not_a_400(dl: tuple, seed: Callable, stand_in: str, category: str, name: str) -> None:
    """KNOWN GAP (current behaviour, pinned) - MH-ISS-03: same unmapped ``ValueError`` as the download filename. The guard works
    (nothing outside the blob store is read); only the status code is wrong."""
    _ob, cl, _cwd = dl
    mid, vid = seed("ver4", f"{stand_in}/fast.bin")
    r = _verify(cl, mid, vid, category, name)
    assert r.status_code == 500 and r.json()["code"] == "internal.unexpected"


def test_verify_field_validation(dl: tuple) -> None:
    _ob, cl, _cwd = dl
    r = cl.post(f"{M}/verify", json={"model_id": "m", "version_id": "v", "storage_category": "", "storage_name": "x"})
    assert r.status_code == 400 and "body.storage_category" in r.json()["details"]["field_errors"]
    r = cl.post(f"{M}/verify", json={})
    assert set(r.json()["details"]["field_errors"]) == {"body.model_id", "body.version_id", "body.storage_category", "body.storage_name"}
    r = cl.post(f"{M}/verify", json={"model_id": "m", "version_id": "v", "storage_category": "x" * 33, "storage_name": "x"})
    assert r.status_code == 400
