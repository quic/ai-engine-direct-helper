# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - Gallery submission API ``/api/gallery/*``, LOCAL half only (GALL-01).

The Gallery feature packages model artifacts and uploads them to an EXTERNAL dashboard. The upload URL is a module constant
(``http://modelgallery.qualcomm.com:3000/api/uploads/submit``, ``gallery_submit.py``) with no setting or environment override, so
the upload cannot be pointed at a stand-in without changing product code.

**Safety rule of this module: no request may reach the upload.** ``POST /submit`` is only ever sent with a body that is rejected
during validation (no ``.dlc``, a path that does not exist, a malformed body). A request that passes validation would start a
background task that POSTs the files to the real dashboard, so the module also asserts that no upload id was ever issued, that every
submit answered 4xx, and that the backend log mentions neither the dashboard host nor any ``gallery.dashboard_*`` code.

NOT covered (deliberately):

* **A submission that passes validation** - packaging, the five-script zip threshold, the ``uploading`` phase, ``done`` / ``error``
  progress, ``gallery.dashboard_http_error`` / ``gallery.dashboard_unreachable`` / ``gallery.upload_failed``. All of them end in a
  call to the external host.
* **``open-file`` on an openable file**: the product calls ``os.startfile`` (the OS opens the file in a desktop program). Only the
  rejections before that call are sent (missing file, a directory, a non-openable extension).
* **``generate-description`` reaching a cloud model**: with a loopback stand-in the product answers ``description_requires_cloud`` on
  purpose (it refuses local endpoints), and a non-loopback endpoint would be a real one. So only the checks BEFORE the model call are
  tested; the cloud call, its error mapping and ``gallery.cloud_model_*`` are not.

Isolation (verified): one throw-away backend; every file lives in a temp folder removed in teardown; the repo is only read. Deleting
is done only on a file the test created.

Observed behaviour worth pinning:

* ``scan`` walks the folder recursively and lists ``.dlc`` / ``.bin`` / ``.md`` / ``.py`` only (``.txt`` / ``.onnx`` are skipped),
  sorted by full path, each ``selected: true`` with a POSIX ``rel_path``. ``model_name`` is the folder name; ``category`` is ``genai``
  when a file of type ``model`` exists, which no scannable extension produces - so it is always ``non_genai`` (GALL-ISS-01);
  ``quant_method`` is the first ``fp16|fp32|w8a16|w4a16|int8`` found in a file name (case-insensitive, lower-cased);
  ``qnn_version`` comes from a ``qairt|qnn ... <version>`` mention in a ``.py|.log|.sh|.bat|.txt`` file.
* Unlike ``/submit``, ``scan`` takes ``.md`` and ``.py`` files too, so a folder with only those is a valid scan.
* ``submit`` validates in this order: at least one ``.dlc`` among the paths, then every path must be an existing FILE. A directory
  is not a file. The ``.dlc`` test is case-insensitive.
* ``open-file`` / ``delete-file`` take any absolute path: ``delete-file`` deletes any existing FILE the backend can reach, with no
  check that it is inside a workdir (GALL-ISS-02, pinned with a file the test created).
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests.e2e._owned_backend import OwnedBackend, owned_backend

pytestmark = pytest.mark.e2e

G = "/api/gallery"
BASE = {"submitter": "e2e", "email": "e2e@example.invalid", "model_name": "e2e-model", "model_category": "non_genai"}
SCAN_KEYS = {"model_name", "category", "qnn_version", "quant_method", "files"}
FILE_KEYS = {"path", "filename", "size", "selected", "type", "rel_path"}


@pytest.fixture(scope="module")
def gal() -> Iterator[tuple[OwnedBackend, httpx.Client]]:
    with owned_backend() as ob:
        yield ob, ob.start()


@pytest.fixture(scope="module")
def cl(gal: tuple[OwnedBackend, httpx.Client]) -> httpx.Client:
    return gal[1]


@pytest.fixture(scope="module", autouse=True)
def nothing_was_uploaded(gal: tuple[OwnedBackend, httpx.Client]) -> Iterator[None]:
    yield
    log = gal[0].log_text().lower()
    assert "modelgallery" not in log and "gallery.dashboard" not in log and "gallery.upload_failed" not in log, \
        "a submit request got past validation and reached the upload"


@pytest.fixture
def work() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(prefix="qai-gal-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def model_dir(work: Path) -> Path:
    (work / "Model_FP16.dlc").write_bytes(b"d" * 10)
    (work / "model_w8a16.dlc").write_bytes(b"d" * 20)
    (work / "weights.bin").write_bytes(b"b" * 5)
    (work / "README.md").write_text("# hello\nA tiny model.", encoding="utf-8")
    (work / "run.py").write_text("# converted with QAIRT SDK v2.48.40\nprint(1)\n", encoding="utf-8")
    (work / "skip.txt").write_text("not scannable", encoding="utf-8")
    (work / "skip.onnx").write_bytes(b"o")
    (work / "sub").mkdir()
    (work / "sub" / "deep.dlc").write_bytes(b"x" * 3)
    (work / "sub" / "model_fp16.dlc").write_bytes(b"y" * 4)
    (work / "empty_dir").mkdir()
    return work


def post_submit(cl: httpx.Client, body: dict) -> httpx.Response:
    """The ONLY place this module posts to /submit (a test greps for any other). Asserts the answer is a client error, i.e. that no
    upload was started: a 2xx means the request passed validation and the product began sending files to the external dashboard."""
    r = cl.post(f"{G}/submit", json=body)
    assert 400 <= r.status_code < 500, f"submit was ACCEPTED ({r.status_code}) - it would have uploaded: {r.text[:200]}"
    assert "upload_id" not in r.json()
    return r


def submit(cl: httpx.Client, paths: list[str], **over: object) -> httpx.Response:
    return post_submit(cl, {**BASE, "file_paths": paths, **over})


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------


def test_scan_lists_the_scannable_files_recursively_with_sizes(cl: httpx.Client, model_dir: Path) -> None:
    r = cl.get(f"{G}/scan", params={"workdir": str(model_dir)})
    assert r.status_code == 200 and set(r.json()) == SCAN_KEYS
    files = r.json()["files"]
    assert all(set(f) == FILE_KEYS and f["selected"] is True for f in files)
    got = {f["rel_path"]: (f["type"], f["size"]) for f in files}
    on_disk = lambda rel: (model_dir / rel).stat().st_size  # noqa: E731 - sizes of the text files depend on the line endings written
    assert got == {"Model_FP16.dlc": ("dlc", 10), "model_w8a16.dlc": ("dlc", 20), "README.md": ("md", on_disk("README.md")),
                   "run.py": ("py", on_disk("run.py")), "sub/deep.dlc": ("dlc", 3), "sub/model_fp16.dlc": ("dlc", 4), "weights.bin": ("bin", 5)}
    assert [f["path"] for f in files] == sorted((f["path"] for f in files), key=lambda s: s.lower()), "sorted by full path, case-insensitively (Windows Path ordering)"
    assert [f["rel_path"] for f in files][:3] == ["Model_FP16.dlc", "model_w8a16.dlc", "README.md"], "README.md sorts among the m... files, not before them"
    assert all(Path(f["path"]).is_file() and f["filename"] == Path(f["path"]).name for f in files)


def test_scan_skips_other_extensions(cl: httpx.Client, model_dir: Path) -> None:
    names = {f["filename"] for f in cl.get(f"{G}/scan", params={"workdir": str(model_dir)}).json()["files"]}
    assert "skip.txt" not in names and "skip.onnx" not in names


def test_scan_infers_name_quantization_and_sdk_version(cl: httpx.Client, model_dir: Path) -> None:
    b = cl.get(f"{G}/scan", params={"workdir": str(model_dir)}).json()
    assert b["model_name"] == model_dir.name
    assert b["quant_method"] == "fp16", "the first quantization word in a file name wins, lower-cased (Model_FP16.dlc)"
    assert b["qnn_version"] == "2.48.40"


@pytest.mark.parametrize("name,expected", [("a_w8a16.dlc", "w8a16"), ("a_W4A16.dlc", "w4a16"), ("a_int8.dlc", "int8"), ("a_fp32.dlc", "fp32"), ("plain.dlc", None)])
def test_scan_quant_method_from_a_file_name(cl: httpx.Client, work: Path, name: str, expected: str | None) -> None:
    (work / name).write_bytes(b"1")
    assert cl.get(f"{G}/scan", params={"workdir": str(work)}).json()["quant_method"] == expected


@pytest.mark.parametrize("names,expected", [
    (("a_w8a16.dlc", "b_fp16.dlc"), "w8a16"),
    (("b_fp16.dlc", "a_w8a16.dlc"), "w8a16"),
    (("a_int8.dlc", "b_fp32.dlc", "c_w4a16.dlc"), "int8"),
])
def test_scan_quant_method_is_the_first_match_in_path_order_not_the_last(cl: httpx.Client, work: Path, names: tuple[str, ...], expected: str) -> None:
    """Files are searched in sorted-path order and the first one carrying a quantization word wins, whatever order they were created in."""
    for n in names:
        (work / n).write_bytes(b"1")
    assert cl.get(f"{G}/scan", params={"workdir": str(work)}).json()["quant_method"] == expected


@pytest.mark.parametrize("text,expected", [("qairt 2.40.1", "2.40.1"), ("QNN-SDK_v2.30", "2.30"), ("qairt_sdk 2.48.40.260702", "2.48.40"), ("no version here", None)])
def test_scan_sdk_version_pattern(cl: httpx.Client, work: Path, text: str, expected: str | None) -> None:
    (work / "build.log").write_text(text, encoding="utf-8")
    assert cl.get(f"{G}/scan", params={"workdir": str(work)}).json()["qnn_version"] == expected


def test_scan_sdk_version_is_not_read_from_markdown(cl: httpx.Client, work: Path) -> None:
    (work / "README.md").write_text("converted with qairt 2.40.1", encoding="utf-8")
    assert cl.get(f"{G}/scan", params={"workdir": str(work)}).json()["qnn_version"] is None


def test_scan_category_is_non_genai_for_every_scannable_set_of_files(cl: httpx.Client, model_dir: Path, work: Path) -> None:
    """Pins GALL-ISS-01: ``genai`` needs a file of type ``model``, which no scannable extension yields."""
    assert cl.get(f"{G}/scan", params={"workdir": str(model_dir)}).json()["category"] == "non_genai"


def test_scan_of_an_empty_folder_is_valid_and_empty(cl: httpx.Client, work: Path) -> None:
    r = cl.get(f"{G}/scan", params={"workdir": str(work)})
    assert r.status_code == 200
    assert r.json() == {"model_name": work.name, "category": "non_genai", "qnn_version": None, "quant_method": None, "files": []}


def test_scan_of_a_folder_with_only_markdown_and_python_is_valid(cl: httpx.Client, work: Path) -> None:
    (work / "a.md").write_text("x", encoding="utf-8")
    (work / "b.py").write_text("x", encoding="utf-8")
    assert {f["type"] for f in cl.get(f"{G}/scan", params={"workdir": str(work)}).json()["files"]} == {"md", "py"}


@pytest.mark.parametrize("kind", ["missing", "a file"])
def test_scan_rejects_a_path_that_is_not_a_directory(cl: httpx.Client, model_dir: Path, kind: str) -> None:
    target = model_dir / ("nope" if kind == "missing" else "README.md")
    r = cl.get(f"{G}/scan", params={"workdir": str(target)})
    assert r.status_code == 400 and r.json()["code"] == "gallery.workdir_not_found" and str(target) in r.json()["message"]


def test_scan_requires_the_workdir_parameter(cl: httpx.Client) -> None:
    r = cl.get(f"{G}/scan")
    assert r.status_code == 400 and r.json()["code"] == "request.validation"
    assert "query.workdir" in r.json()["details"]["field_errors"]


def test_scan_does_not_modify_the_folder(cl: httpx.Client, model_dir: Path) -> None:
    before = sorted((str(p.relative_to(model_dir)), p.stat().st_size) for p in model_dir.rglob("*"))
    cl.get(f"{G}/scan", params={"workdir": str(model_dir)})
    assert sorted((str(p.relative_to(model_dir)), p.stat().st_size) for p in model_dir.rglob("*")) == before


# --------------------------------------------------------------------------
# submit: validation failures ONLY
# --------------------------------------------------------------------------


@pytest.mark.parametrize("files", [[], ["weights.bin"], ["README.md", "run.py"], ["ghost.bin"]])
def test_submit_without_a_dlc_is_rejected_before_any_file_is_looked_at(cl: httpx.Client, model_dir: Path, files: list[str]) -> None:
    r = submit(cl, [str(model_dir / f) for f in files])
    assert r.status_code == 400 and r.json()["code"] == "gallery.dlc_required"
    assert ".dlc" in r.json()["message"]


@pytest.mark.parametrize("extra", ["ghost.bin", "sub", "empty_dir", "ghost.dlc", "X.DLC"])
def test_submit_with_a_path_that_is_not_an_existing_file_is_rejected(cl: httpx.Client, model_dir: Path, extra: str) -> None:
    paths = [str(model_dir / "model_w8a16.dlc")]
    if extra.lower().endswith(".dlc"):
        paths = []
    paths.append(str(model_dir / extra))
    r = submit(cl, paths)
    assert r.status_code == 400 and r.json()["code"] == "gallery.file_not_found" and str(model_dir / extra) in r.json()["message"]


def test_submit_dlc_check_is_case_insensitive_so_an_upper_case_extension_gets_to_the_file_check(cl: httpx.Client, model_dir: Path) -> None:
    r = submit(cl, [str(model_dir / "NOT-THERE.DLC")])
    assert r.json()["code"] == "gallery.file_not_found", "an upper-case .DLC counts as a DLC (otherwise this would be gallery.dlc_required)"


def test_submit_reports_the_first_missing_path_only(cl: httpx.Client, model_dir: Path) -> None:
    r = submit(cl, [str(model_dir / "model_w8a16.dlc"), str(model_dir / "ghost1.bin"), str(model_dir / "ghost2.bin")])
    assert "ghost1.bin" in r.json()["message"] and "ghost2.bin" not in r.json()["message"]


@pytest.mark.parametrize("drop", ["submitter", "email", "model_name", "model_category", "file_paths"])
def test_submit_requires_each_mandatory_field(cl: httpx.Client, model_dir: Path, drop: str) -> None:
    body = {**BASE, "file_paths": [str(model_dir / "model_w8a16.dlc")]}
    body.pop(drop)
    r = post_submit(cl, body)
    assert r.status_code == 400 and r.json()["code"] == "request.validation" and f"body.{drop}" in r.json()["details"]["field_errors"]


def test_submit_with_an_empty_body_lists_every_missing_field(cl: httpx.Client) -> None:
    r = post_submit(cl, {})
    assert r.status_code == 400
    assert set(r.json()["details"]["field_errors"]) == {f"body.{k}" for k in ("submitter", "email", "model_name", "model_category", "file_paths")}


def test_submit_rejects_a_file_paths_that_is_not_a_list(cl: httpx.Client) -> None:
    r = post_submit(cl, {**BASE, "file_paths": "x.dlc"})
    assert r.status_code == 400 and "body.file_paths" in r.json()["details"]["field_errors"]


def test_submit_validation_failures_leave_the_files_untouched(cl: httpx.Client, model_dir: Path) -> None:
    before = sorted((str(p.relative_to(model_dir)), p.read_bytes()) for p in model_dir.rglob("*") if p.is_file())
    submit(cl, [str(model_dir / "model_w8a16.dlc"), str(model_dir / "ghost.bin")])
    submit(cl, [str(model_dir / "weights.bin")])
    assert sorted((str(p.relative_to(model_dir)), p.read_bytes()) for p in model_dir.rglob("*") if p.is_file()) == before


# --------------------------------------------------------------------------
# progress
# --------------------------------------------------------------------------


@pytest.mark.parametrize("upload_id", ["does-not-exist", "0" * 32, "%20", "x" * 200])
def test_progress_of_an_unknown_upload_is_404(cl: httpx.Client, upload_id: str) -> None:
    r = cl.get(f"{G}/submit-progress/{upload_id}")
    assert r.status_code == 404 and r.json()["code"] == "gallery.upload_not_found"
    assert r.json()["details"]["resource_type"] == "upload"


def test_no_upload_was_registered_by_the_rejected_submits(cl: httpx.Client, model_dir: Path) -> None:
    """A rejected submit must not leave a progress entry behind: probe the ids a uuid4 hex could not be, then confirm 404 stays."""
    r = submit(cl, [str(model_dir / "weights.bin")])
    assert "upload_id" not in r.json()
    assert cl.get(f"{G}/submit-progress/{'a' * 32}").status_code == 404


# --------------------------------------------------------------------------
# open-file: rejections before os.startfile ONLY
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["ghost.md", "sub", "empty_dir"])
def test_open_file_of_something_that_is_not_a_file_is_404(cl: httpx.Client, model_dir: Path, name: str) -> None:
    r = cl.post(f"{G}/open-file", json={"path": str(model_dir / name)})
    assert r.status_code == 404 and r.json()["code"] == "gallery.file_not_found"


@pytest.mark.parametrize("name", ["Model_FP16.dlc", "weights.bin", "skip.onnx"])
def test_open_file_refuses_extensions_outside_the_allow_list(cl: httpx.Client, model_dir: Path, name: str) -> None:
    r = cl.post(f"{G}/open-file", json={"path": str(model_dir / name)})
    assert r.status_code == 400 and r.json()["code"] == "gallery.file_type_not_openable"
    assert Path(name).suffix in r.json()["message"]


def test_open_file_requires_a_path(cl: httpx.Client) -> None:
    r = cl.post(f"{G}/open-file", json={})
    assert r.status_code == 400 and "body.path" in r.json()["details"]["field_errors"]


# --------------------------------------------------------------------------
# delete-file: only on files this test created
# --------------------------------------------------------------------------


def test_delete_file_removes_exactly_that_file(cl: httpx.Client, model_dir: Path) -> None:
    before = {p.name for p in model_dir.iterdir()}
    r = cl.post(f"{G}/delete-file", json={"path": str(model_dir / "skip.txt")})
    assert r.status_code == 200 and r.json() == {"success": True}
    assert {p.name for p in model_dir.iterdir()} == before - {"skip.txt"}
    assert (model_dir / "README.md").is_file() and (model_dir / "sub" / "deep.dlc").is_file()


def test_delete_file_twice_the_second_is_404(cl: httpx.Client, model_dir: Path) -> None:
    assert cl.post(f"{G}/delete-file", json={"path": str(model_dir / "skip.txt")}).status_code == 200
    r = cl.post(f"{G}/delete-file", json={"path": str(model_dir / "skip.txt")})
    assert r.status_code == 404 and r.json()["code"] == "gallery.file_not_found"


@pytest.mark.parametrize("name", ["ghost.md", "empty_dir", "sub"])
def test_delete_file_never_removes_a_directory(cl: httpx.Client, model_dir: Path, name: str) -> None:
    r = cl.post(f"{G}/delete-file", json={"path": str(model_dir / name)})
    assert r.status_code == 404 and r.json()["code"] == "gallery.file_not_found"
    assert name == "ghost.md" or (model_dir / name).is_dir()


def test_delete_file_has_no_workdir_boundary(cl: httpx.Client, model_dir: Path) -> None:
    """Pins GALL-ISS-02: any existing file the backend can reach is deleted. Only a file this test made, in its own temp folder."""
    other = Path(tempfile.mkdtemp(prefix="qai-gal-other-"))
    try:
        victim = other / "unrelated.txt"
        victim.write_text("outside any gallery workdir", encoding="utf-8")
        assert cl.post(f"{G}/delete-file", json={"path": str(victim)}).status_code == 200
        assert not victim.exists()
    finally:
        shutil.rmtree(other, ignore_errors=True)


def test_delete_file_requires_a_path(cl: httpx.Client) -> None:
    r = cl.post(f"{G}/delete-file", json={})
    assert r.status_code == 400 and "body.path" in r.json()["details"]["field_errors"]


# --------------------------------------------------------------------------
# generate-description: checks BEFORE the model call
# --------------------------------------------------------------------------


def test_description_for_a_missing_folder_is_400(cl: httpx.Client, work: Path) -> None:
    r = cl.post(f"{G}/generate-description", json={"workdir": str(work / "nope")})
    assert r.status_code == 400 and r.json()["code"] == "gallery.workdir_not_found"


def test_description_for_a_folder_without_markdown_is_404(cl: httpx.Client, work: Path) -> None:
    (work / "a.dlc").write_bytes(b"1")
    r = cl.post(f"{G}/generate-description", json={"workdir": str(work)})
    assert r.status_code == 404 and r.json()["code"] == "gallery.no_doc_found"


@pytest.mark.parametrize("text", ["", "   \n\t"])
def test_description_for_an_empty_document_is_400(cl: httpx.Client, work: Path, text: str) -> None:
    (work / "README.md").write_text(text, encoding="utf-8")
    r = cl.post(f"{G}/generate-description", json={"workdir": str(work)})
    assert r.status_code == 400 and r.json()["code"] == "gallery.doc_empty" and "README.md" in r.json()["message"]


def test_description_without_a_cloud_model_asks_the_user_to_pick_one(cl: httpx.Client, model_dir: Path) -> None:
    for body in ({"workdir": str(model_dir)}, {"workdir": str(model_dir), "model_id": "no-such-model"}):
        r = cl.post(f"{G}/generate-description", json=body)
        assert r.status_code == 400 and r.json()["code"] == "gallery.description_requires_cloud"


def test_description_requires_a_workdir(cl: httpx.Client) -> None:
    r = cl.post(f"{G}/generate-description", json={})
    assert r.status_code == 400 and "body.workdir" in r.json()["details"]["field_errors"]


def test_description_picks_the_document_after_the_checks_in_priority_order(cl: httpx.Client, work: Path) -> None:
    """MODEL_CARD.md beats README.md: an EMPTY model card must be reported, not the non-empty README."""
    (work / "README.md").write_text("real content", encoding="utf-8")
    (work / "MODEL_CARD.md").write_text("", encoding="utf-8")
    r = cl.post(f"{G}/generate-description", json={"workdir": str(work)})
    assert r.json()["code"] == "gallery.doc_empty" and "MODEL_CARD.md" in r.json()["message"]


def test_description_prefers_readme_over_other_markdown_and_the_shallowest_otherwise(cl: httpx.Client, work: Path) -> None:
    (work / "z-other.md").write_text("", encoding="utf-8")
    (work / "README.md").write_text("", encoding="utf-8")
    assert "README.md" in cl.post(f"{G}/generate-description", json={"workdir": str(work)}).json()["message"]
    (work / "README.md").unlink()
    (work / "sub").mkdir()
    (work / "sub" / "a.md").write_text("", encoding="utf-8")
    assert "z-other.md" in cl.post(f"{G}/generate-description", json={"workdir": str(work)}).json()["message"], "the shallowest markdown file wins"


def test_this_module_posts_to_submit_through_one_guarded_helper_only() -> None:
    """Static guard: a future test that posts to /submit directly could pass validation and upload to the real dashboard."""
    import re

    src = Path(__file__).read_text(encoding="utf-8")
    posts = [m.start() for m in re.finditer(r"\.post\(f\"\{G\}/submit\"", src)]
    assert len(posts) == 1, f"{len(posts)} direct POSTs to /submit; only post_submit() may do it"
    assert "def post_submit" in src[max(0, posts[0] - 900):posts[0]]


def test_the_whole_module_never_started_an_upload(gal: tuple[OwnedBackend, httpx.Client]) -> None:
    log = gal[0].log_text().lower()
    assert "modelgallery" not in log and "uploading" not in log
    assert "gallery.dashboard" not in json.dumps(log)
