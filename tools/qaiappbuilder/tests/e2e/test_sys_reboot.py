# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - exit code 75 makes the supervisor restart the API (SYS-08).

``POST /api/system/reboot`` answers 202 and schedules a graceful stop with exit code **75** (``settings.server.reboot_exit_code``, an
external contract). ``qai serve`` (``apps.cli.serve``) is the supervisor: it spawns ``python -m apps.api`` and respawns it whenever the child
exits 75. These tests run the REAL supervisor (``python -m apps.cli.serve --port 8989``) against a throw-away data dir, and talk to the child it
starts. Run directly (no supervisor), the same route simply ends the API process with code 75.

NOT covered (deliberately):

* ``MAX_REBOOT_CYCLES`` (32) and ``MAX_CRASH_RESTARTS`` (5 in 300 s): reaching them needs dozens of restarts or crashes of a 15 s boot each.
* Ctrl+C / ``CTRL_BREAK`` handling of the supervisor and its 35 s graceful-stop window, ``--probe-host``, ``--no-reboot``-style flags.
* ``Start.bat`` (SYS-09, a separate row) and the Windows service form of the supervisor.
* Reboots caused by other routes (``PUT /api/security/mode``, ``policy`` ``requires_reboot``): they go through the same scheduler, which is what
  is covered here.

Isolation (verified): the supervisor and its child are children of the test; the data dir is a throw-away folder; the module kills ONLY the
supervisor tree it started and asserts no process is left on port 8989. A leftover supervisor would respawn the API forever, so the fixture's
teardown kills the whole tree even when a test fails.

Observed behaviour worth pinning:

* ``/reboot`` answers 202 ``{"status": "scheduled", "exit_code": 75}`` and the process exits roughly a second later. ``/exit`` answers 202 with
  ``exit_code: 0`` - a clean stop that the supervisor does NOT turn into a restart.
* The supervisor replaces the child (new PID, new ``start_time``, small ``uptime_seconds``) and stays the same process; the new child serves
  the same data dir, so conversations survive.
* Calling ``/reboot`` twice quickly is coalesced into ONE restart.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import psutil
import pytest

from tests.e2e import conftest as c

pytestmark = pytest.mark.e2e

BASE = f"http://{c.E2E_HOST}:{c.E2E_PORT}"


def _health() -> bool:
    try:
        return httpx.get(BASE + "/api/system/health", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


def _wait_health(seconds: float = 90.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if _health():
            return True
        time.sleep(0.5)
    return False


def _cmd(p: psutil.Process) -> str:
    """``cmdline()`` of a process that may be exiting right now (a reboot is exactly that): treat any psutil error as 'not an API process'."""
    try:
        return " ".join(p.cmdline() or [])
    except (psutil.Error, OSError):
        return ""


def _api_children(sup: psutil.Process) -> list[psutil.Process]:
    """The API processes the supervisor started. On Windows a venv ``python.exe`` is a launcher that starts the real interpreter, so one
    API is TWO ``apps.api`` processes (parent and child); only the outer one counts as "the API"."""
    try:
        every = [p for p in sup.children(recursive=True) if "apps.api" in _cmd(p)]
    except psutil.Error:
        return []
    pids = {p.pid for p in every}
    out = []
    for p in every:
        try:
            if p.ppid() not in pids:
                out.append(p)
        except psutil.Error:
            continue
    return out

def _new_child(sup: psutil.Process, old: set[int], seconds: float = 90.0) -> psutil.Process | None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        fresh = [p for p in _api_children(sup) if p.pid not in old]
        if fresh and _health():
            return fresh[0]
        time.sleep(0.4)
    return None


class Supervised:
    def __init__(self, proc: subprocess.Popen, data: Path, log: Path) -> None:
        self.proc, self.data, self.log = proc, data, log
        self.ps = psutil.Process(proc.pid)

    def client(self) -> httpx.Client:
        cl = httpx.Client(base_url=BASE, timeout=30.0)
        cl.get("/api/system/uptime")
        cl.headers["X-QAI-CSRF"] = cl.cookies.get("qai_csrf")
        return cl


@pytest.fixture
def sup() -> Iterator[Supervised]:
    if not c._port_is_free(c.E2E_HOST, c.E2E_PORT):
        pytest.skip(f"port {c.E2E_PORT} is in use by another process")
    work = Path(tempfile.mkdtemp(prefix="qai-e2e-sup-"))
    data = work / "data"
    data.mkdir()
    env = c._backend_env(data, auth_enabled=False, pycache=work / "pyc")
    log = work / "supervisor.log"
    with log.open("wb") as fh:
        proc = subprocess.Popen([c._python_executable(), "-m", "apps.cli.serve", "--port", str(c.E2E_PORT)], cwd=str(c.REPO_ROOT), env=env,
                                stdout=fh, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    s = Supervised(proc, data, log)
    try:
        assert _wait_health(), "the supervised API never became healthy:\n" + log.read_text(encoding="utf-8", errors="replace")[-800:]
        yield s
    finally:
        try:
            for ch in s.ps.children(recursive=True):
                try:
                    ch.kill()
                except psutil.Error:
                    pass
            s.ps.kill()
        except psutil.Error:
            pass
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not c._port_is_free(c.E2E_HOST, c.E2E_PORT):
            time.sleep(0.3)
        shutil.rmtree(work, ignore_errors=True)
    assert c._port_is_free(c.E2E_HOST, c.E2E_PORT), "the supervised API is still holding the port"


def test_the_supervisor_starts_the_api_as_its_child(sup: Supervised) -> None:
    kids = _api_children(sup.ps)
    assert len(kids) == 1 and "--port" in _cmd(kids[0]) and str(c.E2E_PORT) in _cmd(kids[0])
    assert sup.proc.poll() is None


def test_reboot_answers_202_and_the_child_is_replaced_while_the_supervisor_stays(sup: Supervised) -> None:
    cl = sup.client()
    old = {p.pid for p in _api_children(sup.ps)}
    before = cl.get("/api/system/uptime").json()
    r = cl.post("/api/system/reboot")
    assert r.status_code == 202 and r.json() == {"status": "scheduled", "exit_code": 75}
    new = _new_child(sup.ps, old)
    assert new is not None and new.pid not in old, "a new API process took over"
    after = httpx.get(BASE + "/api/system/uptime", timeout=10).json()
    assert after["start_time"] > before["start_time"] and after["uptime_seconds"] < 40
    assert sup.proc.poll() is None and sup.ps.pid == psutil.Process(sup.proc.pid).pid, "the supervisor itself did not restart"
    assert not [pid for pid in old if psutil.pid_exists(pid)], "the old child is gone"


def test_the_supervisor_logs_the_restart(sup: Supervised) -> None:
    cl = sup.client()
    old = {p.pid for p in _api_children(sup.ps)}
    cl.post("/api/system/reboot")
    assert _new_child(sup.ps, old) is not None
    text = sup.log.read_text(encoding="utf-8", errors="replace")
    assert "75" in text and ("reboot" in text.lower() or "restart" in text.lower()), text[-600:]


def test_data_written_before_the_reboot_is_there_after_it(sup: Supervised) -> None:
    cl = sup.client()
    cid = cl.post("/api/chat/conversations", json={"title": "e2e-survives-reboot"}).json()["id"]
    old = {p.pid for p in _api_children(sup.ps)}
    cl.post("/api/system/reboot")
    assert _new_child(sup.ps, old) is not None
    cl2 = sup.client()
    assert cl2.get(f"/api/chat/conversations/{cid}").json()["title"] == "e2e-survives-reboot"


def test_a_second_reboot_request_right_after_the_first_is_coalesced(sup: Supervised) -> None:
    cl = sup.client()
    old = {p.pid for p in _api_children(sup.ps)}
    a, b = cl.post("/api/system/reboot"), cl.post("/api/system/reboot")
    assert (a.status_code, b.status_code) == (202, 202)
    first = _new_child(sup.ps, old)
    assert first is not None
    time.sleep(6.0)
    kids = _api_children(sup.ps)
    assert [p.pid for p in kids] == [first.pid], "one restart, not two"


def test_the_exit_route_stops_the_api_for_good_even_under_the_supervisor(sup: Supervised) -> None:
    """``POST /api/system/exit`` answers ``exit_code: 0`` (a clean stop, not the reboot code 75): the child ends, the supervisor sees a normal
    exit and ends too instead of restarting it."""
    cl = sup.client()
    r = cl.post("/api/system/exit")
    assert r.status_code == 202 and r.json() == {"status": "scheduled", "exit_code": 0}
    assert sup.proc.wait(timeout=60) == 0, "the supervisor follows the child's clean exit"
    assert not _health() and _api_children(sup.ps) == []

def test_reboot_needs_the_csrf_token(sup: Supervised) -> None:
    bare = httpx.Client(base_url=BASE, timeout=10.0)
    r = bare.post("/api/system/reboot")
    assert r.status_code == 403 and r.json()["code"] == "security.csrf.missing"
    time.sleep(3.0)
    assert _health() and len(_api_children(sup.ps)) == 1, "a refused request must not restart anything"


def test_reboot_is_post_only(sup: Supervised) -> None:
    cl = sup.client()
    assert cl.get("/api/system/reboot").status_code == 404 and cl.put("/api/system/reboot").status_code in (404, 405), "GET is not even routed"
    assert _health()


def test_without_a_supervisor_reboot_ends_the_process_with_code_75() -> None:
    """Run directly (``python -m apps.api``), the route still exits with the contract code - nothing restarts it."""
    if not c._port_is_free(c.E2E_HOST, c.E2E_PORT):
        pytest.skip(f"port {c.E2E_PORT} is in use by another process")
    work = Path(tempfile.mkdtemp(prefix="qai-e2e-sup-"))
    try:
        data = work / "data"
        data.mkdir()
        env = c._backend_env(data, auth_enabled=False, pycache=work / "pyc")
        with (work / "api.log").open("wb") as fh:
            proc = subprocess.Popen([c._python_executable(), "-m", "apps.api", "--port", str(c.E2E_PORT)], cwd=str(c.REPO_ROOT), env=env, stdout=fh, stderr=subprocess.STDOUT)
        try:
            assert _wait_health()
            cl = httpx.Client(base_url=BASE, timeout=30.0)
            cl.get("/api/system/uptime")
            cl.headers["X-QAI-CSRF"] = cl.cookies.get("qai_csrf")
            assert cl.post("/api/system/reboot").status_code == 202
            assert proc.wait(timeout=60) == 75
        finally:
            if proc.poll() is None:
                psutil.Process(proc.pid).kill()
        assert c._port_is_free(c.E2E_HOST, c.E2E_PORT)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_the_reboot_exit_code_is_configurable_and_the_response_follows_it() -> None:
    if not c._port_is_free(c.E2E_HOST, c.E2E_PORT):
        pytest.skip(f"port {c.E2E_PORT} is in use by another process")
    work = Path(tempfile.mkdtemp(prefix="qai-e2e-sup-"))
    try:
        data = work / "data"
        data.mkdir()
        env = c._backend_env(data, auth_enabled=False, pycache=work / "pyc")
        env["QAI_SERVER__REBOOT_EXIT_CODE"] = "77"
        with (work / "api.log").open("wb") as fh:
            proc = subprocess.Popen([c._python_executable(), "-m", "apps.api", "--port", str(c.E2E_PORT)], cwd=str(c.REPO_ROOT), env=env, stdout=fh, stderr=subprocess.STDOUT)
        try:
            assert _wait_health()
            cl = httpx.Client(base_url=BASE, timeout=30.0)
            cl.get("/api/system/uptime")
            cl.headers["X-QAI-CSRF"] = cl.cookies.get("qai_csrf")
            r = cl.post("/api/system/reboot")
            assert r.json() == {"status": "scheduled", "exit_code": 77} and proc.wait(timeout=60) == 77
        finally:
            if proc.poll() is None:
                psutil.Process(proc.pid).kill()
    finally:
        shutil.rmtree(work, ignore_errors=True)
