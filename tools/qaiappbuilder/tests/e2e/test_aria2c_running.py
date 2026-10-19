# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - aria2c management with a binary on PATH: start / status / stop / cancel (MH-05, part 2).

Own module: ONE module-scoped backend (aria2c on its PATH, ``<data>/bin`` present) serves every test, so no test here may start another
backend (the suite waits 30 s for port 8989 and then aborts the session). The no-binary, missing-``bin``-dir and daemon-lifetime cases
are in ``test_aria2c.py``. See that module for what is not covered and for the isolation guarantees (no ``aria2c`` outlives the backend,
port 6800 free afterwards).
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import psutil
import pytest

from tests.e2e import conftest as c
from tests.e2e._owned_backend import OwnedBackend, owned_backend
from tests.e2e.conftest import REPO_ROOT

pytestmark = pytest.mark.e2e

A = "/api/aria2c"
ARIA_DIR = REPO_ROOT / "data" / "bin" / "aria2c"
RPC = 6800


def _aria_pids() -> set[int]:
    return {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower().startswith("aria2c")}


@pytest.fixture(scope="module", autouse=True)
def daemon_ports_and_processes_clean() -> Iterator[None]:
    for port in (RPC, 6810):
        if not c._port_is_free(c.E2E_HOST, port):
            pytest.skip(f"port {port} (aria2c RPC) is in use by another process")
    if not (ARIA_DIR / "aria2c.exe").is_file():
        pytest.skip("data/bin/aria2c/aria2c.exe is not installed")
    before = _aria_pids()
    yield
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and (_aria_pids() - before):
        time.sleep(0.3)
    assert not (_aria_pids() - before), "an aria2c process outlived the backend that started it"
    assert c._port_is_free(c.E2E_HOST, RPC), "port 6800 is still held after the module"


def _wait_stopped(timeout: float = 15.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end and not c._port_is_free(c.E2E_HOST, RPC):
        time.sleep(0.3)


@pytest.fixture(scope="module")
def with_binary() -> Iterator[tuple[OwnedBackend, httpx.Client]]:
    """One backend with aria2c on PATH and ``<data>/bin`` present. ``cwd`` is a temp dir so nothing is written into the repo."""
    with owned_backend() as ob:
        (ob.data_dir / "bin").mkdir(parents=True, exist_ok=True)
        yield ob, ob.start(path_prepend=ARIA_DIR, cwd=Path(tempfile.gettempdir()))


def test_status_before_start_finds_the_binary_and_reports_no_daemon(with_binary: tuple) -> None:
    ob, cl = with_binary
    s = cl.get(f"{A}/status").json()
    assert s["available"] is True and s["can_auto_install"] is False
    assert Path(s["exe_path"]).name.lower() == "aria2c.exe" and Path(s["exe_path"]).resolve() == (ARIA_DIR / "aria2c.exe").resolve()
    assert (s["daemon_running"], s["daemon_pid"]) == (False, None)


def test_start_launches_the_daemon_and_status_follows(with_binary: tuple) -> None:
    ob, cl = with_binary
    before = _aria_pids()
    r = cl.post(f"{A}/start")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["message"] == f"aria2c daemon running on port {RPC}" and isinstance(body["pid"], int)
    assert body["pid"] in _aria_pids() - before, "that PID is a new aria2c process"
    assert not c._port_is_free(c.E2E_HOST, RPC)
    s = cl.get(f"{A}/status").json()
    assert (s["daemon_running"], s["daemon_pid"], s["rpc_port"]) == (True, body["pid"], RPC)
    proc = psutil.Process(body["pid"])
    cmd = proc.cmdline()
    assert "--enable-rpc" in cmd and f"--rpc-listen-port={RPC}" in cmd and "--quiet=true" in cmd


def test_start_again_returns_the_same_daemon(with_binary: tuple) -> None:
    ob, cl = with_binary
    first = cl.post(f"{A}/start").json()
    again = cl.post(f"{A}/start").json()
    assert first["status"] == again["status"] == "ok" and first["pid"] == again["pid"]
    assert len(_aria_pids() & {first["pid"]}) == 1


def test_the_daemon_answers_json_rpc_on_loopback_only(with_binary: tuple) -> None:
    ob, cl = with_binary
    cl.post(f"{A}/start")
    r = httpx.post(f"http://127.0.0.1:{RPC}/jsonrpc", json={"jsonrpc": "2.0", "id": "1", "method": "aria2.getVersion"}, timeout=5)
    assert r.status_code == 200 and r.json()["result"]["version"].startswith("1.")
    listeners = [conn for conn in psutil.Process(cl.post(f"{A}/start").json()["pid"]).net_connections(kind="tcp") if conn.laddr.port == RPC and conn.status == "LISTEN"]
    assert listeners and all(conn.laddr.ip in ("127.0.0.1", "::1") for conn in listeners), "not reachable from other machines"


def test_cancel_of_an_unknown_task_while_the_daemon_runs(with_binary: tuple) -> None:
    ob, cl = with_binary
    cl.post(f"{A}/start")
    r = cl.post(f"{A}/cancel/zzz")
    assert r.status_code == 200 and r.json() == {"cancelled": False, "task_id": "zzz"}
    assert cl.get(f"{A}/status").json()["daemon_running"] is True


def test_stop_ends_the_process_and_frees_the_port_and_a_new_start_works(with_binary: tuple) -> None:
    ob, cl = with_binary
    pid = cl.post(f"{A}/start").json()["pid"]
    r = cl.post(f"{A}/stop")
    assert r.status_code == 200 and r.json() == {"status": "ok", "message": "aria2c daemon stopped"}
    _wait_stopped()
    assert pid not in _aria_pids() and c._port_is_free(c.E2E_HOST, RPC)
    s = cl.get(f"{A}/status").json()
    assert (s["daemon_running"], s["daemon_pid"]) == (False, None) and s["available"] is True
    again = cl.post(f"{A}/start").json()
    assert again["status"] == "ok" and again["pid"] != pid
    cl.post(f"{A}/stop")
    _wait_stopped()


def test_stop_twice_is_fine(with_binary: tuple) -> None:
    ob, cl = with_binary
    cl.post(f"{A}/start")
    for _ in range(2):
        assert cl.post(f"{A}/stop").json()["status"] == "ok"
    _wait_stopped()
