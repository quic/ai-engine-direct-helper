# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - starting a download when ``aria2c`` is not on the backend's PATH (MH-02, part 2).

Own module: ``test_downloads.py`` keeps a module-scoped backend on port 8989 for its whole run, and the suite refuses to
start a second backend while one is alive (it never kills a process it did not start). Nothing here touches the network:
the stand-in server is only used to prove that no request was made.
"""

from __future__ import annotations

import hashlib
import http.server
import sqlite3
import threading
from collections.abc import Iterator

import pytest

from tests.e2e._owned_backend import owned_backend

pytestmark = pytest.mark.e2e

M = "/api/model-catalog"
FAST = b"E2E-MODEL-BYTES" * 1000
SHA_FAST = hashlib.sha256(FAST).hexdigest()


class _Handler(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        _Handler.hits.append(self.path)
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


def _start(cl, mid, vid, filename):
    return cl.post(f"{M}/download", json={"model_id": mid, "version_id": vid, "target_filename": filename})


def test_without_aria2c_on_the_path_start_is_a_clean_503_and_the_job_stays_queued(stand_in: str) -> None:
    """The product finds ``aria2c`` only through ``PATH``. Without it the request fails at once with a service error and the
    job is left ``queued`` in the list (it never runs)."""
    with owned_backend() as ob:
        cl = ob.start()
        cl.post(f"{M}/entries", json={"model_id": "m", "name": "m", "provider": "local", "source_url": "https://example.invalid/x",
                                      "description": "d", "tags": []})
        con = sqlite3.connect(str(ob.data_dir / "db" / "qai.db"), timeout=15)
        con.execute("INSERT INTO model_catalog_version (id,parent_model_id,checksum_algorithm,checksum_value,size_bytes,manifest_url,status)"
                    " VALUES ('v','m','sha256',?,?,?,'published')", (SHA_FAST, len(FAST), f"{stand_in}/fast.bin"))
        con.commit()
        con.close()
        hits_before = list(_Handler.hits)
        r = _start(cl, "m", "v", "x.bin")
        assert r.status_code == 503 and r.json()["code"] == "model_catalog.aria2c.start_failed"
        assert r.json()["details"] == {"service": "aria2c"} and "binary not found: 'aria2c'" in r.json()["message"]
        jobs = cl.get(f"{M}/download-jobs").json()["jobs"]
        assert [j["state"] for j in jobs] == ["queued"]
        assert _Handler.hits == hits_before, "nothing reached the network"
