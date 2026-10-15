# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 — runtime endpoint file and SPA hosting (FEATURE-INVENTORY SYS-05 / SYS-06).

SYS-05  ``<data_dir>/runtime/server.endpoint.json`` matches the live server.
SYS-06  ``/`` serves index.html; unknown client-router paths fall back to the
        SPA; real files under ``frontend/dist`` are served verbatim with a
        correct MIME type; the API surface is never shadowed by the fallback.

Behaviour is taken from ``src/qai/platform/process/runtime_endpoint.py`` and
``apps/api/_spa_mount.py``. These tests assert on the COMPILED ``frontend/dist``
that is checked out; they skip (not fail) when it has not been built, because a
missing bundle is a build-state problem, not a server regression (the 503
maintenance page for that case is SYS-07).
"""

from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest

from tests.e2e.conftest import REPO_ROOT, Backend

pytestmark = pytest.mark.e2e

DIST = REPO_ROOT / "frontend" / "dist"

needs_dist = pytest.mark.skipif(
    not (DIST / "index.html").is_file(),
    reason="frontend/dist/index.html not built (run Build.bat); see SYS-07 for the 503 page",
)


# ---------------------------------------------------------------------------
# SYS-05  server.endpoint.json
# ---------------------------------------------------------------------------


def _read_endpoint(backend: Backend) -> dict:
    path = backend.data_dir / "runtime" / "server.endpoint.json"
    assert path.is_file(), f"endpoint file not written at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _listener_pids(port: int) -> set[int]:
    import psutil

    return {
        c.pid
        for c in psutil.net_connections(kind="tcp")
        if c.laddr and c.laddr.port == port and c.status == "LISTEN" and c.pid
    }


def _descendant_pids(pid: int) -> set[int]:
    import psutil

    return {p.pid for p in psutil.Process(pid).children(recursive=True)}


def test_sys05_endpoint_file_matches_the_running_server(backend: Backend):
    ep = _read_endpoint(backend)
    assert ep["schema_version"] == 1
    assert ep["host"] == "127.0.0.1"
    assert ep["port"] == backend.port
    assert ep["url"] == f"http://127.0.0.1:{backend.port}"
    datetime.fromisoformat(ep["started_at"])


def test_sys05_endpoint_pid_is_the_process_serving_the_port(backend: Backend):
    """The recorded pid is the real listener, and it is the process WE started.

    NOTE: ``backend.pid`` is the pid of what we spawned, which on Windows is the
    venv's ``python.exe`` launcher; the server proper is its child. So we do not
    compare the pids for equality - we require that the endpoint pid is (a) the
    process actually listening on the port and (b) us or one of our descendants,
    which rules out a stale file from some other server.
    """
    ep = _read_endpoint(backend)
    assert ep["pid"] in _listener_pids(backend.port)
    assert ep["pid"] == backend.pid or ep["pid"] in _descendant_pids(backend.pid)


def test_sys05_endpoint_url_is_reachable(backend: Backend):
    """The advertised URL really serves traffic (what Start.bat relies on)."""
    url = _read_endpoint(backend)["url"]
    assert httpx.get(f"{url}/api/system/health", timeout=10.0).status_code == 200


def test_sys05_no_partial_tmp_files_left_behind(backend: Backend):
    """The write is atomic (tmp file + os.replace); no ``.tmp`` may linger."""
    leftovers = list((backend.data_dir / "runtime").glob("server.endpoint.json.*.tmp"))
    assert leftovers == []


# ---------------------------------------------------------------------------
# SYS-06  SPA hosting
# ---------------------------------------------------------------------------


@needs_dist
def test_sys06_root_serves_index_html(http: httpx.Client):
    r = http.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.content == (DIST / "index.html").read_bytes()


@needs_dist
def test_sys06_index_html_is_never_cached(http: httpx.Client):
    """index.html references hashed chunk names; a cached copy 404s after a rebuild."""
    cc = http.get("/").headers["cache-control"].lower()
    assert "no-store" in cc and "no-cache" in cc


@needs_dist
@pytest.mark.parametrize("path", ["/chat", "/settings", "/some/deep/client/route"])
def test_sys06_unknown_client_paths_fall_back_to_the_spa(http: httpx.Client, path: str):
    r = http.get(path, headers={"accept": "text/html"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.content == (DIST / "index.html").read_bytes()


@needs_dist
def test_sys06_real_dist_files_are_served_verbatim_not_as_html(http: httpx.Client):
    """A file that exists under dist/ must win over the SPA fallback."""
    r = http.get("/index.html")
    assert r.status_code == 200
    assert r.content == (DIST / "index.html").read_bytes()


@needs_dist
@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        (".js", "application/javascript"),
        (".css", "text/css"),
    ],
)
def test_sys06_assets_have_the_correct_mime_type(
    http: httpx.Client, suffix: str, expected: str
):
    """Windows can map .js to text/plain, which browsers refuse as an ES module."""
    asset = next(p for p in sorted((DIST / "assets").iterdir()) if p.suffix == suffix)
    r = http.get(f"/assets/{asset.name}")
    assert r.status_code == 200
    assert r.headers["content-type"].split(";")[0].strip() == expected
    assert r.content == asset.read_bytes()


@needs_dist
def test_sys06_missing_asset_is_404_not_the_spa(http: httpx.Client):
    """A stale chunk name must 404 loudly instead of returning HTML as JS."""
    r = http.get("/assets/this-chunk-does-not-exist-abc123.js")
    assert r.status_code == 404
    assert "text/html" not in r.headers.get("content-type", "")


@needs_dist
@pytest.mark.parametrize("path", ["/api/no-such-endpoint", "/v1/no-such-endpoint"])
def test_sys06_api_surface_is_not_shadowed_by_the_spa_fallback(
    http: httpx.Client, path: str
):
    r = http.get(path, headers={"accept": "text/html"})
    assert r.status_code == 404
    assert "text/html" not in r.headers.get("content-type", "")


@needs_dist
def test_sys06_path_traversal_cannot_escape_dist(http: httpx.Client):
    """Encoded ``..`` must never return a file from outside frontend/dist."""
    secret_marker = "[tool.pytest.ini_options]"  # lives in pyproject.toml, outside dist/
    for path in ("/..%2f..%2fpyproject.toml", "/%2e%2e/%2e%2e/pyproject.toml"):
        r = http.get(path, headers={"accept": "text/html"})
        assert secret_marker not in r.text, f"traversal leaked a file via {path}"
