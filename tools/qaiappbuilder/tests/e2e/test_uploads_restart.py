# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - uploads: what survives a backend restart (CHAT-07, part 2).

Own module, own backends (``_owned_backend.py``): the upload index is IN MEMORY and rebuilt from the file names at
startup, so the only way to see the rebuild is to restart on the same data directory.

Observed behaviour worth pinning:

* The index is rebuilt from ``<category>/<ULID>_<name>`` and ``<category>/<conv_id>/<ULID>_<name>`` (one folder deep).
* A ``conv_id`` with a separator (``a/b``) puts the file two folders deep, which the rebuild does not scan: the upload
  is **still on disk but no longer listed or deletable** after a restart.
* The original file name is gone after a restart: only the sanitised stored name is left, so ``My File (1).png`` comes
  back as ``My_File__1_.png``. ``created_at`` becomes the file's modification time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e._owned_backend import owned_backend

pytestmark = pytest.mark.e2e

A = "/api/uploads"


def _post(cl, cat: str, name: str, data: bytes = b"123", **form: str) -> dict:
    r = cl.post(f"{A}/{cat}", files={"file": (name, data, "application/octet-stream")}, data=form or None)
    assert r.status_code == 201, r.text
    return r.json()


def _by_id(cl) -> dict[str, dict]:
    return {u["id"]: u for u in cl.get(A).json()["uploads"]}


def test_uploads_are_listed_again_after_a_restart() -> None:
    with owned_backend() as ob:
        cl = ob.start()
        ids = {
            "flat": _post(cl, "image", "a.png")["id"],
            "conv": _post(cl, "model", "m.bin", conv_id="c1")["id"],
            "dataset": _post(cl, "dataset", "d.csv")["id"],
        }
        before = _by_id(cl)
        ob.stop()
        cl = ob.start()
        after = _by_id(cl)
        assert set(after) == set(ids.values())
        for uid in ids.values():
            assert (after[uid]["category"], after[uid]["filename"], after[uid]["conv_id"], after[uid]["size_bytes"], after[uid]["path"]) == \
                   (before[uid]["category"], before[uid]["filename"], before[uid]["conv_id"], before[uid]["size_bytes"], before[uid]["path"])
        assert [u["filename"] for u in cl.get(A, params={"conv_id": "c1"}).json()["uploads"]] == ["m.bin"]


def test_an_upload_can_be_deleted_after_a_restart() -> None:
    with owned_backend() as ob:
        cl = ob.start()
        body = _post(cl, "image", "a.png")
        ob.stop()
        cl = ob.start()
        assert cl.delete(f"{A}/{body['id']}").status_code == 200
        assert not Path(body["path"]).exists() and cl.get(A).json()["uploads"] == []


def test_the_original_file_name_comes_back_sanitised_after_a_restart() -> None:
    """KNOWN GAP (current behaviour, pinned): the index is rebuilt from the stored (sanitised) name, so the name the
    user uploaded is lost - ``中文名.png`` becomes ``___.png``."""
    with owned_backend() as ob:
        cl = ob.start()
        a = _post(cl, "image", "My File (1).png")["id"]
        b = _post(cl, "image", "中文名.png")["id"]
        assert [u["filename"] for u in cl.get(A).json()["uploads"] if u["id"] in (a, b)] == ["中文名.png", "My File (1).png"]
        ob.stop()
        cl = ob.start()
        names = {u["id"]: u["filename"] for u in cl.get(A).json()["uploads"]}
        assert names[a] == "My_File__1_.png" and names[b] == "___.png"


def test_an_upload_under_a_nested_conv_id_vanishes_from_the_list_after_a_restart() -> None:
    """KNOWN GAP (current behaviour, pinned): ``conv_id="a/b"`` is two folders deep and the rebuild scans one. The file
    stays on disk, but it is no longer listed and ``DELETE`` answers 404, so it can never be removed through the API."""
    with owned_backend() as ob:
        cl = ob.start()
        body = _post(cl, "model", "n.bin", conv_id="a/b")
        assert body["id"] in _by_id(cl)
        ob.stop()
        cl = ob.start()
        assert body["id"] not in _by_id(cl)
        assert Path(body["path"]).exists(), "the bytes are still there"
        assert cl.delete(f"{A}/{body['id']}").status_code == 404


def test_files_that_do_not_follow_the_naming_convention_are_ignored_on_restart() -> None:
    with owned_backend() as ob:
        cl = ob.start()
        good = _post(cl, "image", "a.png")
        folder = Path(good["path"]).parent
        ob.stop()
        (folder / "hand-copied.png").write_bytes(b"x")
        (folder / "01M5_short.png").write_bytes(b"x")
        (ob.data_dir / "blobs" / "uploads" / "unknown-category").mkdir()
        (ob.data_dir / "blobs" / "uploads" / "unknown-category" / ("0" * 26 + "_x.bin")).write_bytes(b"x")
        cl = ob.start()
        assert list(_by_id(cl)) == [good["id"]]


def test_a_hand_placed_file_with_a_valid_name_is_adopted_on_restart() -> None:
    with owned_backend() as ob:
        cl = ob.start()
        folder = Path(_post(cl, "image", "a.png")["path"]).parent
        ob.stop()
        uid = "0" * 25 + "1"
        (folder / f"{uid}_adopted.png").write_bytes(b"abc")
        cl = ob.start()
        got = _by_id(cl)[uid]
        assert (got["category"], got["filename"], got["size_bytes"], got["conv_id"]) == ("image", "adopted.png", 3, None)
        assert cl.delete(f"{A}/{uid}").status_code == 200 and not (folder / f"{uid}_adopted.png").exists()
