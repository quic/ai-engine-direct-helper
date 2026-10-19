# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""A backend the test OWNS (start, read the exit code, restart on the same data dir).

``conftest._start_backend`` yields a backend for a whole module and hides its exit code. Some features
deliberately make the process exit (security ``runtime-config`` reboot-class fields exit with code 75 so the
supervisor can respawn it) or need a restart on the SAME data directory to prove persistence. This helper
reuses the conftest's environment, port rules and health wait, but hands the ``Popen`` to the test.

Rules it keeps (same as the shared fixture): port 8989 only, abort the session if it is occupied (never kill
an occupant), temp data dir, stop only our own process tree.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from tests.e2e import conftest as c

__all__ = ["OwnedBackend", "owned_backend"]


@dataclass
class OwnedBackend:
    work: Path
    data_dir: Path
    proc: subprocess.Popen[bytes] | None = None
    log_path: Path | None = None
    clients: list[httpx.Client] = field(default_factory=list)
    _runs: int = 0

    @property
    def base_url(self) -> str:
        return f"http://{c.E2E_HOST}:{c.E2E_PORT}"

    def start(self, *, cwd: Path | None = None, path_prepend: Path | None = None) -> httpx.Client:
        """Start (or restart) the backend on this data dir and return a client with a CSRF cookie.

        ``cwd`` runs the backend from another folder (default: the repo root). Needed when something the backend spawns
        writes relative to its working directory (aria2c downloads), so a test cannot litter the repo.
        ``path_prepend`` puts a folder first on the backend's ``PATH`` (the repo's ``data/bin/aria2c``)."""
        if self.proc is not None and self.proc.poll() is None:
            raise RuntimeError("backend already running")
        self._wait_port_free()
        self._runs += 1
        self.log_path = self.work / f"backend-{self._runs}.log"
        env = c._backend_env(self.data_dir, auth_enabled=False, pycache=self.work / "pyc")
        if path_prepend is not None:
            env["PATH"] = str(path_prepend) + os.pathsep + env.get("PATH", "")
        with self.log_path.open("wb") as fh:
            self.proc = subprocess.Popen(
                [c._python_executable(), "-m", "apps.api", "--port", str(c.E2E_PORT)],
                cwd=str(cwd or c.REPO_ROOT), env=env, stdout=fh, stderr=subprocess.STDOUT,
            )
        c._wait_healthy(self.proc, self.base_url, self.log_path)
        client = httpx.Client(base_url=self.base_url, timeout=30.0)
        client.get("/api/system/uptime")
        token = client.cookies.get("qai_csrf")
        if token:  # absent when the backend runs with CSRF protection disabled
            client.headers["X-QAI-CSRF"] = token
        self.clients.append(client)
        return client

    def wait_exit(self, timeout: float = 60.0) -> int | None:
        """Wait for the backend to exit by itself and return its exit code (``None`` on timeout)."""
        assert self.proc is not None
        deadline = time.monotonic() + timeout
        while self.proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.3)
        return self.proc.poll()

    def stop(self) -> None:
        """Stop only our own process (and children). Never aborts the session: this runs in cleanup paths."""
        for cl in self.clients:
            try:
                cl.close()
            except Exception:  # noqa: BLE001 - cleanup must not raise
                pass
        self.clients.clear()
        if self.proc is not None:
            c._stop(self.proc)
        # Wait (bounded) for the port to be released so a following start() does not race it. Not fatal here:
        # start() performs the strict check and is the only place allowed to abort the session.
        deadline = time.monotonic() + 30.0
        while not c._port_is_free(c.E2E_HOST, c.E2E_PORT) and time.monotonic() < deadline:
            time.sleep(0.3)

    def log_text(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path else ""

    def _wait_port_free(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while not c._port_is_free(c.E2E_HOST, c.E2E_PORT):
            if time.monotonic() > deadline:
                pytest.exit(
                    f"E2E aborted: port {c.E2E_PORT} is in use by {c._describe_port_owner(c.E2E_PORT)} "
                    "and not by this test; this suite never kills a process it did not start.",
                    returncode=2,
                )
            time.sleep(0.3)


def _remove_tree(path: Path, attempts: int = 8) -> None:
    """Remove ``path``, retrying: a worker the backend spawned (the App Builder sticky worker) can still hold a
    ``.pyc`` in ``<work>/pyc`` for a moment after the backend is gone, and Windows refuses to delete an open file.
    Never raises - a leftover temp folder must not fail a test; the module's own assertions cover real leaks."""
    for i in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.5 * (i + 1))


@contextmanager
def owned_backend() -> Iterator[OwnedBackend]:
    """Context manager: a throw-away work dir; the process and the dir are always cleaned up."""
    work = Path(tempfile.mkdtemp(prefix="qai-e2e-"))
    data = work / "data"
    data.mkdir()
    ob = OwnedBackend(work=work, data_dir=data)
    try:
        yield ob
    finally:
        try:
            ob.stop()
        finally:
            _remove_tree(work)
