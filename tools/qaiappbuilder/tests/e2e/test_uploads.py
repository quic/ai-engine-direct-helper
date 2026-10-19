# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - chat attachment / image upload ``/api/uploads`` (CHAT-07).

Endpoints (``interfaces/http/routes/uploads.py`` over ``qai/platform/uploads``): ``POST /api/uploads/{image,model,
code,dataset,audio,voice}``, the legacy ``POST /api/upload/code``, ``GET /api/uploads`` and ``DELETE /api/uploads/{id}``.

NOT covered (deliberately):

* **The 2 GB / 500 MB / 100 MB / 50 MB ceilings.** Only the 10 MB image and 10 MB legacy-code caps are hit - each
  is a ~10 MB request. The larger ones need hundreds of MB of RAM and disk per test; they are the same
  ``validate_category_size`` table, whose values are asserted indirectly by the 10 MB boundary.
* **Concurrent uploads and a partial write (disk full).** The store writes with ``Path.write_bytes`` and updates its
  in-memory index afterwards; a crash between the two is not exercised.
* **What the chat does with an uploaded file** (sending it to a model). That is CHAT-02/05, which need an LLM stand-in.

Isolation (verified): files land under ``<data_dir>/blobs/uploads`` in the throw-away data dir. The ``conv_id`` of a
test is NEVER allowed to climb more than two levels (``../..`` ends in ``blobs/``, still inside the data dir); an earlier
exploration run with six levels wrote a file OUTSIDE the data dir and had to be cleaned up by hand, so the traversal
tests below prove the escape with the shallowest case that shows it. A fixture refuses to run unless the data dir is a
``qai-e2e-*`` temp dir, wipes ``blobs/`` extras before and after, and an autouse check fails the module if any file
named ``e2e-marker-*`` exists anywhere outside the uploads root after a test.

Observed behaviour worth pinning (it differs from what one might assume):

* **``conv_id`` is joined onto the upload directory unchecked** (``/model`` and ``/dataset`` only): ``../../x`` writes
  into ``blobs/x`` outside the uploads root. The returned path and ``GET /uploads`` show it faithfully, and ``DELETE``
  removes it, so nothing is hidden - but the destination is attacker-chosen.
* A ``conv_id`` of ``" "``, one over ~140 characters, or containing ``:`` is a **500** (``FileNotFoundError`` from
  ``mkdir``), as is a **file name over ~150 characters** once the data directory is a typical Windows path: the
  stored name is ``<26-char id>_<up to 200 chars>`` and the full path passes 260.
* After a **restart** the index is rebuilt from the file names: uploads stored under a nested ``conv_id`` (``a/b``)
  disappear from the list, and the original file name comes back in its sanitised form (``My File (1).png`` ->
  ``My_File__1_.png``).
* ``GET /uploads`` returns at most 50 entries; ``image`` ignores a ``conv_id`` field; ``/api/uploads/code`` has no
  extension allow-list (``.exe`` is accepted), only the legacy ``/api/upload/code`` does.
"""

from __future__ import annotations

import io
import shutil
import tarfile
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from tests.e2e._owned_backend import owned_backend  # noqa: F401  (used by the restart module)
from tests.e2e.conftest import Backend

pytestmark = pytest.mark.e2e

A = "/api/uploads"
MB = 1024 * 1024
MARK = "e2e-marker-"


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------


@pytest.fixture
def api(http: httpx.Client) -> httpx.Client:
    http.get("/api/system/uptime")
    http.headers["X-QAI-CSRF"] = http.cookies.get("qai_csrf")
    return http


@pytest.fixture
def root(backend: Backend) -> Path:
    return backend.data_dir / "blobs" / "uploads"


@pytest.fixture(autouse=True)
def clean_state(backend: Backend, root: Path, http: httpx.Client) -> Iterator[None]:
    """The store keeps an IN-MEMORY index in the backend process, so removing files from disk is not enough:
    every upload a test made is deleted through the API (which clears the index entry and the file)."""
    data = backend.data_dir
    assert "qai-e2e-" in str(data), f"refusing to touch a non-throw-away data dir: {data}"
    http.get("/api/system/uptime")
    http.headers["X-QAI-CSRF"] = http.cookies.get("qai_csrf")

    def _wipe() -> None:
        # The list is capped at 50, so loop until it is empty.
        for _ in range(20):
            items = http.get(A).json()["uploads"]
            if not items:
                break
            for u in items:
                http.delete(f"{A}/{u['id']}")
        shutil.rmtree(root, ignore_errors=True)
        blobs = data / "blobs"
        if blobs.exists():
            for p in blobs.iterdir():
                if p.name.startswith("e2e-esc"):
                    shutil.rmtree(p, ignore_errors=True)

    _wipe()
    yield
    stray = [p for p in data.rglob(f"*{MARK}*") if root not in p.parents]
    outside = [p for p in data.parent.rglob(f"*{MARK}*") if data not in p.parents]
    _wipe()
    assert not outside, f"a test wrote outside the throw-away data dir: {outside}"
    for p in stray:  # inside the data dir but outside uploads/ - only the deliberate traversal tests do this
        assert "e2e-esc" in str(p), f"unexpected file outside the uploads root: {p}"

def _post(api: httpx.Client, cat: str, name: str, data: bytes = b"x", **form: str) -> httpx.Response:
    return api.post(f"{A}/{cat}", files={"file": (name, data, "application/octet-stream")}, data=form or None)


def _ok(r: httpx.Response) -> dict:
    assert r.status_code == 201, r.text
    return r.json()


def _listed(api: httpx.Client, **params: str) -> list[dict]:
    r = api.get(A, params=params or None)
    assert r.status_code == 200, r.text
    return r.json()["uploads"]


def _rel(root: Path, path: str) -> str:
    return str(Path(path).resolve().relative_to(root.resolve())).replace("\\", "/")


def _zip(files: dict[str, bytes]) -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return bio.getvalue()


def _tar(files: dict[str, bytes], mode: str = "w") -> bytes:
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode=mode) as t:
        for n, d in files.items():
            ti = tarfile.TarInfo(n)
            ti.size = len(d)
            t.addfile(ti, io.BytesIO(d))
    return bio.getvalue()


# --------------------------------------------------------------------------
# upload: categories
# --------------------------------------------------------------------------


@pytest.mark.parametrize("cat", ["image", "model", "code", "audio", "voice"])
def test_each_category_stores_the_bytes_and_returns_id_path_size(api: httpx.Client, root: Path, cat: str) -> None:
    body = _ok(_post(api, cat, "My File.bin", b"0123456789"))
    assert set(body) == {"id", "path", "size"} and body["size"] == 10 and len(body["id"]) == 26
    p = Path(body["path"])
    assert p.read_bytes() == b"0123456789"
    assert _rel(root, body["path"]) == f"{cat}/{body['id']}_My_File.bin"


def test_dataset_single_file_returns_the_extended_shape(api: httpx.Client, root: Path) -> None:
    body = _ok(_post(api, "dataset", "d.csv", b"1,2"))
    assert set(body) == {"id", "path", "size", "count", "dir", "files"}
    assert body["count"] == 1 and body["size"] == 3 and Path(body["dir"]) == Path(body["path"]).parent
    assert body["files"] == [{"id": body["id"], "path": body["path"], "size": 3, "filename": "d.csv"}]


def test_an_empty_file_is_accepted(api: httpx.Client) -> None:
    body = _ok(_post(api, "image", "empty.png", b""))
    assert body["size"] == 0 and Path(body["path"]).read_bytes() == b""


def test_ids_are_unique_and_time_ordered(api: httpx.Client) -> None:
    ids = [_ok(_post(api, "image", f"{i}.png"))["id"] for i in range(5)]
    assert len(set(ids)) == 5 and ids == sorted(ids)


def test_unknown_category_and_wrong_method(api: httpx.Client) -> None:
    assert _post(api, "video", "a.mp4").status_code in (404, 405)
    assert api.get(f"{A}/image").status_code in (404, 405)


def test_missing_file_and_empty_file_name_are_validation_errors(api: httpx.Client) -> None:
    r = api.post(f"{A}/image", data={"x": "1"})
    assert r.status_code == 400 and "body.file" in r.json()["details"]["field_errors"]
    r = _post(api, "image", "")
    assert r.status_code == 400 and "body.file" in r.json()["details"]["field_errors"]


# --------------------------------------------------------------------------
# upload: size limits
# --------------------------------------------------------------------------


def test_image_limit_is_10_mb_inclusive(api: httpx.Client, root: Path) -> None:
    assert _post(api, "image", "ok.png", b"x" * (10 * MB)).status_code == 201
    r = _post(api, "image", "big.png", b"x" * (10 * MB + 1))
    assert r.status_code == 413 and r.json() == {"detail": "File exceeds maximum size of 10 MB for category 'image'."}
    assert not any("big.png" in p.name for p in root.rglob("*")), "a rejected upload is not stored"
    assert len(_listed(api)) == 1


# --------------------------------------------------------------------------
# upload: file name handling
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sent", "stored", "listed"),
    [
        ("a b/c.txt", "c.txt", "a b/c.txt"),
        ("..\\..\\evil.txt", "evil.txt", "..\\..\\evil.txt"),
        ("../../evil.txt", "evil.txt", "../../evil.txt"),
        ("C:\\Windows\\win.ini", "win.ini", "win.ini"),
        (".hidden", "upload.hidden", ".hidden"),
        ("a:b*c?.txt", "a_b_c_.txt", "a:b*c?.txt"),
        ("中文名.png", "___.png", "中文名.png"),
        ("My File (1).png", "My_File__1_.png", "My File (1).png"),
        ("NUL", "NUL", "NUL"),
        ("con.txt", "con.txt", "con.txt"),
    ],
)
def test_the_stored_name_is_sanitised_and_the_original_is_listed(api: httpx.Client, root: Path, sent: str, stored: str, listed: str) -> None:
    body = _ok(_post(api, "image", sent))
    assert _rel(root, body["path"]) == f"image/{body['id']}_{stored}"
    assert [u["filename"] for u in _listed(api) if u["id"] == body["id"]] == [listed]
    assert Path(body["path"]).parent == root / "image", "the name never changes the folder"


def test_a_file_name_cannot_escape_the_category_folder(api: httpx.Client, backend: Backend, root: Path) -> None:
    body = _ok(_post(api, "image", "../../../../" + MARK + "name.txt"))
    assert Path(body["path"]).parent == root / "image"
    assert not list(backend.data_dir.parent.rglob(f"{MARK}name.txt")) or all(root in p.parents for p in backend.data_dir.parent.rglob(f"{MARK}name.txt"))


def test_reserved_windows_device_names_are_stored_with_the_id_prefix(api: httpx.Client) -> None:
    """The ``<id>_`` prefix makes ``NUL`` / ``con.txt`` ordinary file names, so they are safe on disk."""
    for name in ("NUL", "con.txt", "PRN", "aux"):
        body = _ok(_post(api, "image", name, b"x"))
        assert Path(body["path"]).read_bytes() == b"x"


def test_the_same_file_name_twice_makes_two_files(api: httpx.Client) -> None:
    a, b = _ok(_post(api, "image", "same.png", b"1")), _ok(_post(api, "image", "same.png", b"2"))
    assert a["path"] != b["path"] and Path(a["path"]).read_bytes() == b"1" and Path(b["path"]).read_bytes() == b"2"


def test_a_very_long_file_name_is_a_500_not_a_truncation(api: httpx.Client) -> None:
    """KNOWN GAP (current behaviour, pinned): the sanitised name is cut at 200 characters but the full path is not
    checked against the Windows 260 limit, so a ~160-character name fails with ``FileNotFoundError``. The exact
    threshold depends on the data directory length; 300 characters is over it for any realistic path."""
    r = _post(api, "image", "y" * 296 + ".txt")
    assert r.status_code == 500 and r.json()["code"] == "internal.unexpected"
    assert api.get("/api/system/health").status_code == 200, "the backend survives"


# --------------------------------------------------------------------------
# conv_id (model, dataset)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("cat", ["model", "dataset"])
def test_conv_id_puts_the_file_in_a_per_conversation_folder_and_filters_the_list(api: httpx.Client, root: Path, cat: str) -> None:
    body = _ok(_post(api, cat, "m.bin", b"x", conv_id="conv-1"))
    assert _rel(root, body["path"]) == f"{cat}/conv-1/{body['id']}_m.bin"
    _ok(_post(api, cat, "other.bin", b"x"))
    assert [u["filename"] for u in _listed(api, conv_id="conv-1")] == ["m.bin"]
    assert [u["conv_id"] for u in _listed(api, conv_id="conv-1")] == ["conv-1"]
    assert _listed(api, conv_id="nope") == []


def test_an_empty_conv_id_means_no_conversation(api: httpx.Client, root: Path) -> None:
    body = _ok(_post(api, "model", "m.bin", b"x", conv_id=""))
    assert _rel(root, body["path"]) == f"model/{body['id']}_m.bin"
    assert [u["conv_id"] for u in _listed(api) if u["id"] == body["id"]] == [None]


def test_image_audio_voice_and_code_ignore_conv_id(api: httpx.Client, root: Path) -> None:
    for cat in ("image", "audio", "voice", "code"):
        body = _ok(_post(api, cat, "a.bin", b"x", conv_id="conv-x"))
        assert _rel(root, body["path"]) == f"{cat}/{body['id']}_a.bin"
    assert _listed(api, conv_id="conv-x") == []


def test_list_conv_id_filter_with_an_empty_value_matches_nothing(api: httpx.Client) -> None:
    _ok(_post(api, "model", "m.bin", b"x"))
    assert _listed(api, conv_id="") == [], "an empty filter is a filter for conversation '', not 'no filter'"


@pytest.mark.parametrize(("conv_id", "where"), [("a/b", "model/a/b"), ("a\\b", "model/a/b")])
def test_conv_id_with_separators_creates_nested_folders(api: httpx.Client, root: Path, conv_id: str, where: str) -> None:
    body = _ok(_post(api, "model", "m.bin", b"x", conv_id=conv_id))
    assert Path(body["path"]).exists() and root in Path(body["path"]).resolve().parents
    assert _rel(root, body["path"]).startswith(where.split("/")[0])


def _unmounted_drive() -> str:
    import string
    mounted = {p.drive.rstrip(":").upper() for p in map(Path, [f"{c}:\\" for c in string.ascii_uppercase]) if p.exists()}
    free = [c for c in "ZYXWVUTSRQPONMLKJIHGFEDCBA" if c not in mounted]
    if not free:
        pytest.skip("every drive letter is mounted on this machine")
    return free[0]


def test_an_absolute_conv_id_is_not_rejected_as_invalid_input(api: httpx.Client) -> None:
    """KNOWN GAP - SECURITY (current behaviour, pinned): ``conv_id="C:\\x"`` makes the store write to ``C:\\x`` -
    an arbitrary location. That is proven by a manual run, NOT by this suite (the first version of this test did it and
    left ``C:\\x`` behind). Here the drive is one that is not mounted, so nothing can be written: the request fails in
    ``mkdir`` with a 500 instead of a 400 'invalid conv_id', which shows the value reaches the filesystem unchecked."""
    r = _post(api, "model", "m.bin", b"x", conv_id=f"{_unmounted_drive()}:\\e2e-never-created")
    assert r.status_code == 500 and r.json()["code"] == "internal.unexpected"
    assert _listed(api) == []

def test_conv_id_is_a_path_traversal(api: httpx.Client, backend: Backend, root: Path) -> None:
    """KNOWN GAP - SECURITY (current behaviour, pinned): ``conv_id`` is joined onto the category folder without
    validation. ``../../e2e-esc`` writes into ``blobs/e2e-esc`` - OUTSIDE the uploads root (still inside the
    throw-away data dir; deeper values escape the data dir, which this suite never sends). The response path,
    ``GET /uploads`` and ``DELETE`` all report the real location, so it is not hidden - but the destination is the
    caller's choice. If ``conv_id`` is validated this becomes a 400 and the file stays inside ``uploads/``."""
    body = _ok(_post(api, "model", MARK + "t.bin", b"PWN", conv_id="../../e2e-esc"))
    real = Path(body["path"]).resolve()
    assert real == (backend.data_dir / "blobs" / "e2e-esc" / f"{body['id']}_{MARK}t.bin").resolve()
    assert root.resolve() not in real.parents, "outside the uploads root"
    assert real.read_bytes() == b"PWN"
    assert [u["conv_id"] for u in _listed(api, conv_id="../../e2e-esc")] == ["../../e2e-esc"]
    assert api.delete(f"{A}/{body['id']}").status_code == 200 and not real.exists()


def test_conv_id_dot_dot_writes_into_the_parent_of_the_category_folder(api: httpx.Client, root: Path) -> None:
    """KNOWN GAP - SECURITY (current behaviour, pinned): one level up stays inside ``uploads/`` but lands next to
    the category folders, where the index rebuild no longer finds it as a file of that category."""
    body = _ok(_post(api, "model", MARK + "up.bin", b"x", conv_id=".."))
    assert Path(body["path"]).resolve().parent == root.resolve()


def test_conv_id_dataset_is_equally_open(api: httpx.Client, backend: Backend) -> None:
    """KNOWN GAP - SECURITY (current behaviour, pinned): the dataset route has the same hole."""
    body = _ok(_post(api, "dataset", MARK + "d.csv", b"1", conv_id="../../e2e-esc2"))
    assert (backend.data_dir / "blobs" / "e2e-esc2").is_dir()
    assert api.delete(f"{A}/{body['id']}").status_code == 200


@pytest.mark.parametrize("conv_id", [" ", "x" * 300, "a:b"])
def test_unusable_conv_ids_are_a_500(api: httpx.Client, conv_id: str) -> None:
    """KNOWN GAP (current behaviour, pinned): a folder name Windows cannot create is reported as
    ``internal.unexpected`` instead of a 4xx, and nothing is stored."""
    r = _post(api, "model", "m.bin", b"x", conv_id=conv_id)
    assert r.status_code == 500 and r.json()["code"] == "internal.unexpected"
    assert _listed(api) == []
    assert api.get("/api/system/health").status_code == 200


# --------------------------------------------------------------------------
# dataset archives
# --------------------------------------------------------------------------


def test_zip_is_extracted_one_record_per_file_and_directories_are_skipped(api: httpx.Client) -> None:
    body = _ok(_post(api, "dataset", "d.zip", _zip({"a.csv": b"1,2", "sub/b.csv": b"3,4", "empty_dir/": b""})))
    assert body["count"] == 2 and sorted(f["filename"] for f in body["files"]) == ["a.csv", "b.csv"] or sorted(f["filename"] for f in body["files"]) == ["a.csv", "sub/b.csv"]
    assert body["id"] == body["files"][0]["id"] and body["size"] == body["files"][0]["size"]
    for f in body["files"]:
        assert Path(f["path"]).exists()
    assert len(_listed(api)) == 2, "each extracted file is its own upload"


@pytest.mark.parametrize("name", ["d.tar.gz", "d.TGZ", "d.tar", "D.ZIP"])
def test_archive_extensions_are_recognised_case_insensitively(api: httpx.Client, name: str) -> None:
    if name.lower().endswith(".zip"):
        data = _zip({"k.txt": b"k"})
    elif name.lower().endswith(".tar"):
        data = _tar({"k.txt": b"k"})
    else:
        data = _tar({"k.txt": b"k"}, "w:gz")
    body = _ok(_post(api, "dataset", name, data))
    assert body["count"] == 1 and body["files"][0]["filename"] == "k.txt"


def test_other_extensions_are_stored_as_they_are(api: httpx.Client) -> None:
    for name, data in (("data.csv", b"1,2"), ("d.rar", b"Rar!"), ("d.7z", b"7z")):
        body = _ok(_post(api, "dataset", name, data))
        assert body["count"] == 1 and body["files"][0]["filename"] == name and Path(body["path"]).read_bytes() == data


@pytest.mark.parametrize(
    ("members", "detail"),
    [
        ({"../evil.txt": b"x"}, "Archive contains unsafe member path: ../evil.txt"),
        ({"/abs.txt": b"x"}, "Archive contains unsafe member path: /abs.txt"),
        ({"C:/abs.txt": b"x"}, "Archive contains unsafe member path: C:/abs.txt"),
        ({"a/../../b.txt": b"x"}, "Archive contains unsafe member path: a/../../b.txt"),
    ],
)
def test_unsafe_zip_members_are_rejected(api: httpx.Client, root: Path, members: dict, detail: str) -> None:
    r = _post(api, "dataset", "d.zip", _zip(members))
    assert r.status_code == 400 and r.json() == {"detail": detail}
    assert not list(root.rglob("*.txt")), "nothing was written"


def test_a_zip_with_one_unsafe_member_writes_none_of_its_files(api: httpx.Client, root: Path) -> None:
    r = _post(api, "dataset", "mix.zip", _zip({"ok1.txt": b"1", "../bad.txt": b"2", "ok2.txt": b"3"}))
    assert r.status_code == 400
    assert not list(root.rglob("ok*.txt")) and _listed(api) == [], "validation happens before extraction"


def test_a_path_that_walks_up_and_back_down_inside_the_archive_is_allowed(api: httpx.Client) -> None:
    body = _ok(_post(api, "dataset", "d.zip", _zip({"a/../b.txt": b"ok"})))
    assert body["count"] == 1
    assert body["files"][0]["filename"] == "a/../b.txt", "the listed name is the member path as written"
    assert Path(body["files"][0]["path"]).name.endswith("_b.txt") and Path(body["files"][0]["path"]).parent.name == "dataset"


@pytest.mark.parametrize(
    ("name", "data", "needle"),
    [
        ("d.zip", b"not a zip", "Extraction failed: File is not a zip file"),
        ("d.tar", b"not a tar", "Extraction failed: file could not be opened successfully"),
    ],
)
def test_corrupt_archives_are_400(api: httpx.Client, root: Path, name: str, data: bytes, needle: str) -> None:
    r = _post(api, "dataset", name, data)
    assert r.status_code == 400 and needle in r.json()["detail"]
    assert _listed(api) == []


def test_an_empty_archive_is_400(api: httpx.Client) -> None:
    r = _post(api, "dataset", "d.zip", _zip({}))
    assert r.status_code == 400 and r.json() == {"detail": "Dataset archive contained no files."}


def test_unsafe_tar_members_are_rejected_before_anything_is_stored(api: httpx.Client, root: Path) -> None:
    """The path guard runs for tar too, not only for zip (a regular file named ``../x`` is not a link)."""
    for member in ("../evil.txt", "/abs.txt", "a/../../b.txt"):
        r = _post(api, "dataset", "d.tar", _tar({"ok.txt": b"1", member: b"2"}))
        assert r.status_code == 400 and r.json() == {"detail": f"Archive contains unsafe member path: {member}"}
    assert _listed(api) == [] and not list(root.rglob("*.txt"))

def test_a_tar_symlink_is_rejected(api: httpx.Client) -> None:
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w") as t:
        ti = tarfile.TarInfo("link")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "/etc/passwd"
        t.addfile(ti)
    r = _post(api, "dataset", "d.tar", bio.getvalue())
    assert r.status_code == 400 and r.json() == {"detail": "Archive contains unsupported member type: link"}


def test_same_basenames_inside_an_archive_become_distinct_files(api: httpx.Client) -> None:
    body = _ok(_post(api, "dataset", "dups.zip", _zip({"dup.txt": b"1", "sub/dup.txt": b"2"})))
    names = [Path(f["path"]).name for f in body["files"]]
    assert body["count"] == 2 and len(set(names)) == 2 and all(n.endswith("_dup.txt") for n in names)


def test_the_dataset_size_limit_is_checked_on_the_upload_not_the_extracted_files(api: httpx.Client) -> None:
    """A 10 MB image cap does not apply to datasets: 11 MB is fine here."""
    assert _post(api, "dataset", "big.csv", b"x" * (11 * MB)).status_code == 201


# --------------------------------------------------------------------------
# legacy POST /api/upload/code
# --------------------------------------------------------------------------


def _legacy(api: httpx.Client, name: str, data: bytes = b"x") -> httpx.Response:
    return api.post("/api/upload/code", files={"file": (name, data, "text/plain")})


@pytest.mark.parametrize("name", ["a.py", "Makefile", "Dockerfile", "noext", "A.PY", "x.dockerfile", "x.makefile", "r.md", "c.json"])
def test_legacy_code_upload_accepts_allow_listed_extensions_and_extensionless_names(api: httpx.Client, root: Path, name: str) -> None:
    r = _legacy(api, name, b"print(1)")
    assert r.status_code == 200
    j = r.json()
    assert set(j) == {"path", "filename", "size"} and j["filename"] == name and j["size"] == 8
    assert Path(j["path"]).parent == root / "code" and Path(j["path"]).read_bytes() == b"print(1)"


@pytest.mark.parametrize("name", ["x.exe", "a.tar.gz", "a.py.exe", "p.dll", "s.bat"])
def test_legacy_code_upload_rejects_other_extensions_in_chinese(api: httpx.Client, root: Path, name: str) -> None:
    r = _legacy(api, name)
    suffix = Path(name).suffix
    assert r.status_code == 400 and r.json() == {"detail": f"不支持的文件格式 '{suffix}'，请上传常见代码或文本文件。"}
    assert not list(root.rglob("*")), "nothing stored"


def test_legacy_code_upload_keeps_the_callers_file_name_but_sanitises_the_stored_one(api: httpx.Client, root: Path) -> None:
    j = _legacy(api, "..\\..\\e.py").json()
    assert j["filename"] == "..\\..\\e.py" and Path(j["path"]).name.endswith("_e.py") and Path(j["path"]).parent == root / "code"
    j = _legacy(api, ".py").json()
    assert Path(j["path"]).name.endswith("_upload.py")


def test_legacy_code_upload_limit_is_10_mb_inclusive_with_a_chinese_message(api: httpx.Client) -> None:
    assert _legacy(api, "big.py", b"x" * (10 * MB)).status_code == 200
    r = _legacy(api, "big.py", b"x" * (10 * MB + 1))
    assert r.status_code == 413 and r.json() == {"detail": "文件过大（10240 KB），上限 10 MB"}


def test_legacy_code_upload_without_a_name_is_a_validation_error(api: httpx.Client) -> None:
    r = _legacy(api, "")
    assert r.status_code == 400 and "body.file" in r.json()["details"]["field_errors"]


def test_the_generic_code_route_has_no_extension_allow_list(api: httpx.Client) -> None:
    """KNOWN GAP / DESIGN QUESTION (current behaviour, pinned): ``/api/uploads/code`` takes an ``.exe``; only the
    legacy ``/api/upload/code`` filters. Which route the chat UI posts to decides whether this matters."""
    assert _post(api, "code", "x.exe", b"MZ").status_code == 201
    assert _legacy(api, "x.exe", b"MZ").status_code == 400


# --------------------------------------------------------------------------
# list / delete
# --------------------------------------------------------------------------


def test_list_shape_and_newest_first(api: httpx.Client) -> None:
    ids = [_ok(_post(api, "image", f"{i}.png"))["id"] for i in range(3)]
    lst = _listed(api)
    assert [u["id"] for u in lst] == list(reversed(ids))
    assert set(lst[0]) == {"id", "category", "filename", "size_bytes", "path", "created_at", "conv_id"}
    assert (lst[0]["category"], lst[0]["size_bytes"], lst[0]["conv_id"]) == ("image", 1, None)


def test_list_is_capped_at_50_entries(api: httpx.Client) -> None:
    for i in range(52):
        _ok(_post(api, "image", f"n{i}.png"))
    lst = _listed(api)
    assert len(lst) == 50 and lst[0]["filename"] == "n51.png"


def test_delete_removes_the_file_and_the_entry(api: httpx.Client) -> None:
    body = _ok(_post(api, "image", "d.png", b"x"))
    r = api.delete(f"{A}/{body['id']}")
    assert r.status_code == 200 and r.json() == {"status": "deleted"}
    assert not Path(body["path"]).exists() and body["id"] not in [u["id"] for u in _listed(api)]


def test_delete_twice_and_unknown_are_404(api: httpx.Client) -> None:
    body = _ok(_post(api, "image", "d.png"))
    assert api.delete(f"{A}/{body['id']}").status_code == 200
    for uid in (body["id"], "NOPE"):
        r = api.delete(f"{A}/{uid}")
        assert r.status_code == 404 and r.json() == {"detail": f"Upload '{uid}' not found."}


def test_delete_still_succeeds_when_the_file_was_already_removed_from_disk(api: httpx.Client) -> None:
    body = _ok(_post(api, "image", "d.png"))
    Path(body["path"]).unlink()
    assert api.delete(f"{A}/{body['id']}").status_code == 200
    assert api.delete(f"{A}/{body['id']}").status_code == 404


@pytest.mark.parametrize("seg", ["a/b", ".."])
def test_delete_ids_with_path_characters_do_not_match_the_route(api: httpx.Client, root: Path, seg: str) -> None:
    keep = _ok(_post(api, "image", "keep.png"))
    assert api.delete(f"{A}/{seg}").status_code in (404, 405)
    assert Path(keep["path"]).exists()
