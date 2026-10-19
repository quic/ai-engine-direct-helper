# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Keep the backend's log and process state when a test fails.

Every backend the suite starts writes ``backend*.log`` into a throw-away temp dir that is deleted when the
module ends, so a failure that only shows up as an ``httpx`` error (connection reset, read timeout) used to lose
the only evidence of what the server was doing. This module keeps a registry of the live backends and, when a
test fails, copies each one's log next to a small ``.txt`` describing the process at that moment.

Nothing is written for passing tests. Where it goes: ``$QAI_E2E_KEEP_DIR`` (``run_e2e`` points it at
``<report dir>/backend-logs``), else ``<repo>/data/e2e-reports/backend-logs`` (git-ignored).

It only OBSERVES: it never touches the backend process and never raises into the test run.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["register", "unregister", "snapshot_failure", "keep_dir", "mark_done", "is_done"]


@dataclass
class _Entry:
    label: str
    pid_of: Callable[[], int | None]
    log_of: Callable[[], Path | None]
    returncode_of: Callable[[], int | None]
    port: int


_LOCK = threading.Lock()
_LIVE: dict[int, _Entry] = {}
_NEXT = [0]
_COUNT = [0]
_DONE: set[str] = set()   # test ids already snapshotted by the owned_backend context (before it tore the backend down)


def mark_done(nodeid: str) -> None:
    _DONE.add(nodeid)


def is_done(nodeid: str) -> bool:
    return nodeid in _DONE


def register(label: str, *, pid_of, log_of, returncode_of, port: int) -> int:
    """Remember a running backend. Returns a handle for :func:`unregister`."""
    with _LOCK:
        _NEXT[0] += 1
        _LIVE[_NEXT[0]] = _Entry(label, pid_of, log_of, returncode_of, port)
        return _NEXT[0]


def unregister(handle: int) -> None:
    with _LOCK:
        _LIVE.pop(handle, None)


def keep_dir(repo_root: Path) -> Path:
    env = os.environ.get("QAI_E2E_KEEP_DIR")
    return Path(env) if env else repo_root / "data" / "e2e-reports" / "backend-logs"


def _slug(text: str, limit: int = 90) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:limit] or "test"


def _port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def _process_lines(pid: int | None) -> list[str]:
    if not pid:
        return ["process: (no pid)"]
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover
        return [f"process: pid {pid} (psutil missing, no detail)"]
    lines: list[str] = []
    try:
        p = psutil.Process(pid)
        started = _dt.datetime.fromtimestamp(p.create_time()).strftime("%H:%M:%S")
        with p.oneshot():
            lines.append(f"process: pid {pid} status={p.status()} started={started} threads={p.num_threads()}")
            try:
                mi = p.memory_info()
                lines.append(f"memory: rss={mi.rss // 2**20} MB")
                lines.append(f"open handles/fds: {p.num_handles() if hasattr(p, 'num_handles') else p.num_fds()}")
            except psutil.Error as exc:
                lines.append(f"memory/handles: unavailable ({type(exc).__name__})")
        try:
            conns = p.net_connections(kind="inet")
        except AttributeError:  # psutil < 6
            conns = p.connections(kind="inet")
        except psutil.Error:
            conns = []
        by_state: dict[str, int] = {}
        for cn in conns:
            by_state[cn.status] = by_state.get(cn.status, 0) + 1
        lines.append(f"sockets by state: {by_state or '(none)'}")
        kids = p.children(recursive=True)
        lines.append(f"children: {[(k.pid, k.name()) for k in kids] or '(none)'}")
    except psutil.NoSuchProcess:
        lines.append(f"process: pid {pid} NO LONGER EXISTS")
    except psutil.Error as exc:
        lines.append(f"process: pid {pid} could not be inspected ({type(exc).__name__}: {exc})")
    return lines


def snapshot_failure(repo_root: Path, nodeid: str, phase: str, error: str) -> list[Path]:
    """Copy every live backend's log + a state description. Never raises. Returns the files written."""
    written: list[Path] = []
    try:
        with _LOCK:
            entries = list(_LIVE.values())
        _COUNT[0] += 1
        now = _dt.datetime.now()
        dest = keep_dir(repo_root)
        dest.mkdir(parents=True, exist_ok=True)
        stem = f"{now:%H%M%S}-{_COUNT[0]:03d}-{_slug(nodeid.split('::', 1)[-1])}"
        header = [
            f"test:   {nodeid}",
            f"phase:  {phase}",
            f"time:   {now:%Y-%m-%d %H:%M:%S.%f}",
            f"error:  {error[:600]}",
            f"live backends registered: {len(entries)}",
            "",
        ]
        if not entries:
            header.append("(no backend was registered: the failing test did not use a suite backend)")
        body: list[str] = []
        for i, en in enumerate(entries, 1):
            pid, log, rc = en.pid_of(), en.log_of(), en.returncode_of()
            body.append(f"[{i}] {en.label}")
            body.append(f"    exit code: {rc if rc is not None else '(still running)'}")
            body.append(f"    port {en.port} accepting connections: {_port_listening(en.port)}")
            body.extend("    " + ln for ln in _process_lines(pid))
            if log and Path(log).exists():
                target = dest / f"{stem}.backend{i}.log"
                shutil.copyfile(log, target)
                written.append(target)
                body.append(f"    log: {target.name} ({target.stat().st_size} bytes, copied at failure time)")
            else:
                body.append("    log: (not found)")
            body.append("")
        info = dest / f"{stem}.txt"
        info.write_text("\n".join(header + body), encoding="utf-8")
        written.append(info)
    except Exception:  # noqa: BLE001 - diagnostics must never break the run
        pass
    return written
