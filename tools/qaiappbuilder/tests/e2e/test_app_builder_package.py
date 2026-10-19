# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - App Builder: packaging an app project into a zip (AB-11, part 2).

``POST /api/app-builder/apps/{id}/package`` returns a job id; ``GET .../package/{job}/progress`` (SSE) streams ``progress`` frames
and a terminal ``done`` ({zip_path, size_bytes}); the zip lands in ``<workspace>/app_builder_packages/<id>-<YYYYMMDD-HHMMSS>.zip``.

**The workspace is NOT under the data dir.** It defaults to ``C:/WoS_AI`` (the model-builder workspace, the user's real work folder).
Every test therefore starts its backend with ``QAI_WORKSPACE__MODEL_ROOT`` pointing at a throw-away temp folder, and the module
asserts ``C:/WoS_AI`` has the same listing before and after. An earlier exploration run packaged an app WITHOUT that override and
would have written into ``C:/WoS_AI``; it did not only because the call hit a different failure first - see KNOWN-ISSUES APP-ISS-05.

NOT covered (deliberately):

* **Packaging real models** (weights + pack manifests copied under ``models/<id>/`` and ``pack/<id>/``): no model is bundled here; the
  ``include_models`` switch and the warnings for a missing model path are the only parts covered.
* **Whether the packaged app runs on another machine** (``run.bat`` is checked as text only; it is never executed).
* The WebSocket twin of the progress stream.

Observed behaviour worth pinning:

* The zip holds ``app.yaml``, ``README.md``, ``backend/``, ``frontend/``, ``vendor/``, a regenerated ``run.bat`` and ``requirements.txt``
  (with ``qai-appbuilder>=<version>`` appended), ``RUNNING.md``, ``package_manifest.json`` and a ``shared/`` folder of framework helpers.
  Anything else in the app folder (``data/``, ``outputs/``, ``tests/`` ...) is silently left out.
* Excluded anywhere in the tree: ``.venv``, ``venv``, ``__pycache__``, ``.pytest_cache``, ``.mypy_cache``, ``.ruff_cache``, ``.git``, ``package``,
  ``uploads``, ``node_modules``, any ``logs`` folder, and ``*.pyc|pyo|pyd``.
* The zip name has one-second resolution: two packages in the same second are the SAME file (APP-ISS-06).
* A finished job is forgotten once its stream has been read to the end: a second read is a 404.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from tests.e2e._owned_backend import OwnedBackend, owned_backend
from tests.e2e.test_chat_stream import _set_env

pytestmark = pytest.mark.e2e

AB = "/api/app-builder"
REAL_WORKSPACE = Path("C:/WoS_AI")
APP_PY = "from fastapi import FastAPI\napp = FastAPI()\n\n\n@app.get('/health')\ndef health():\n    return {'ok': True}\n"


def _listing(p: Path) -> list[str] | None:
    if not p.exists():
        return None
    return sorted(str(x.relative_to(p)) for x in p.rglob("*") if x.is_file() and "pytest" not in x.parts)


_REAL_AT_START = _listing(REAL_WORKSPACE)


@pytest.fixture(scope="module", autouse=True)
def the_real_workspace_is_untouched() -> Iterator[None]:
    yield
    assert _listing(REAL_WORKSPACE) == _REAL_AT_START, "a test wrote into the real C:/WoS_AI workspace"


@pytest.fixture(scope="module")
def pkg() -> Iterator[tuple[OwnedBackend, httpx.Client, Path]]:
    ws = Path(tempfile.mkdtemp(prefix="qai-e2e-ws-"))
    old = _set_env({"QAI_WORKSPACE__MODEL_ROOT": str(ws)})
    try:
        with owned_backend() as ob:
            yield ob, ob.start(), ws
    finally:
        _set_env(old)
        shutil.rmtree(ws, ignore_errors=True)


def _mk(root: Path, name: str, *, yaml: str | None = None, files: dict[str, str] | None = None) -> Path:
    d = root / name
    (d / "backend").mkdir(parents=True, exist_ok=True)
    (d / "backend" / "__init__.py").write_text("")
    (d / "backend" / "main.py").write_text(APP_PY)
    (d / "app.yaml").write_text(yaml if yaml is not None else f"id: {name}\nname: {name.upper()}\ndescription: about {name}\n")
    for rel, text in (files or {}).items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    return d


@pytest.fixture
def new_app(pkg: tuple) -> Iterator[Callable[..., Path]]:
    ob, cl, _ws = pkg
    made: list[str] = []

    def _new(name: str, **kw: object) -> Path:
        made.append(name)
        return _mk(ob.data_dir / "app_builder", name, **kw)

    yield _new
    for n in made:
        cl.delete(f"{AB}/apps/{n}")


def _package(cl: httpx.Client, app: str, *, seconds: float = 60.0) -> tuple[str, list[tuple[str, object]]]:
    r = cl.post(f"{AB}/apps/{app}/package")
    assert r.status_code == 202, r.text
    job = r.json()["job_id"]
    return job, _frames(cl, app, job, seconds)


def _frames(cl: httpx.Client, app: str, job: str, seconds: float = 60.0) -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    buf = ""
    with cl.stream("GET", f"{AB}/apps/{app}/package/{job}/progress", timeout=httpx.Timeout(10.0, read=seconds)) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"
        for chunk in r.iter_text():
            buf += chunk
            while "\n\n" in buf:
                frame, buf = buf.split("\n\n", 1)
                f = dict(l.split(": ", 1) for l in frame.splitlines() if ": " in l)
                data = f.get("data", "")
                out.append((f.get("event", ""), json.loads(data) if data.startswith("{") else data))
    return out


def _done(frames: list) -> dict:
    (d,) = [d for e, d in frames if e == "done"]
    return d


def _zip(done: dict) -> zipfile.ZipFile:
    return zipfile.ZipFile(done["zip_path"])


# --------------------------------------------------------------------------
# the job and its stream
# --------------------------------------------------------------------------


def test_start_returns_202_with_a_job_id(pkg: tuple, new_app: Callable) -> None:
    _ob, cl, _ws = pkg
    new_app("e2e-pk-start")
    r = cl.post(f"{AB}/apps/e2e-pk-start/package")
    assert r.status_code == 202 and list(r.json()) == ["job_id"] and len(r.json()["job_id"]) == 32
    _frames(cl, "e2e-pk-start", r.json()["job_id"])


def test_progress_runs_through_the_phases_in_order_and_ends_with_done(pkg: tuple, new_app: Callable) -> None:
    _ob, cl, _ws = pkg
    new_app("e2e-pk-phases", files={"frontend/index.html": "<h1>x</h1>"})
    _job, fr = _package(cl, "e2e-pk-phases")
    names = [e for e, _ in fr]
    assert names[:-1] == ["progress"] * (len(names) - 1) and names[-1] == "done"
    phases = [d["phase"] for e, d in fr if e == "progress"]
    assert phases[0] == "collecting" and phases[-2:] == ["writing_zip", "done"]
    order = ["collecting", "copying_app", "copying_models", "writing_zip", "done"]
    assert [order.index(p) for p in phases] == sorted(order.index(p) for p in phases), phases
    pcts = [d["percent"] for e, d in fr if e == "progress"]
    assert pcts == sorted(pcts) and pcts[0] == 0.0 and pcts[-1] == 100.0
    last = [d for e, d in fr if e == "progress"][-1]
    assert last["is_complete"] is True and set(last) == {"phase", "percent", "message", "size_bytes", "zip_path", "is_complete"}
    assert all(d["is_complete"] is False for e, d in fr if e == "progress" and d["phase"] != "done")


def test_done_names_the_zip_inside_the_workspace_and_its_size(pkg: tuple, new_app: Callable) -> None:
    _ob, cl, ws = pkg
    new_app("e2e-pk-done")
    _job, fr = _package(cl, "e2e-pk-done")
    done = _done(fr)
    assert set(done) == {"zip_path", "size_bytes"}
    z = Path(done["zip_path"])
    assert z.parent == ws / "app_builder_packages" and z.is_file() and z.stat().st_size == done["size_bytes"] > 0
    import re

    assert re.fullmatch(r"e2e-pk-done-\d{8}-\d{6}\.zip", z.name)
    assert not list(z.parent.glob(".*.part")), "no half-written file is left behind"


def test_a_finished_job_is_forgotten_and_its_progress_becomes_a_404(pkg: tuple, new_app: Callable) -> None:
    """Reading the stream to its end consumes the job: a second read is the same 404 as an id that never existed."""
    _ob, cl, _ws = pkg
    new_app("e2e-pk-replay")
    job, _fr = _package(cl, "e2e-pk-replay")
    r = cl.get(f"{AB}/apps/e2e-pk-replay/package/{job}/progress")
    assert r.status_code == 404 and r.json()["code"] == "app_builder.package_job_not_found" and r.json()["details"]["resource_id"] == job

def test_progress_of_an_unknown_job_is_a_404_and_cancel_is_idempotent(pkg: tuple, new_app: Callable) -> None:
    _ob, cl, _ws = pkg
    new_app("e2e-pk-unknown")
    r = cl.get(f"{AB}/apps/e2e-pk-unknown/package/nojob/progress")
    assert r.status_code == 404 and r.json()["code"] == "app_builder.package_job_not_found"
    assert r.json()["details"] == {"resource_type": "package_job", "resource_id": "nojob"}
    for _ in range(2):
        assert cl.delete(f"{AB}/apps/e2e-pk-unknown/package/nojob").status_code == 204


def test_the_progress_route_ignores_which_app_the_job_belongs_to(pkg: tuple, new_app: Callable) -> None:
    """KNOWN GAP (current behaviour, pinned) - APP-ISS-07: the job is looked up by id alone; any ``{app_id}`` in the path works."""
    _ob, cl, _ws = pkg
    new_app("e2e-pk-a")
    r = cl.post(f"{AB}/apps/e2e-pk-a/package")
    fr = _frames(cl, "totally-another-app", r.json()["job_id"])
    assert _done(fr)["zip_path"].split("\\")[-1].startswith("e2e-pk-a-")


def test_cancelling_a_job_before_reading_it_drops_it(pkg: tuple, new_app: Callable) -> None:
    _ob, cl, _ws = pkg
    new_app("e2e-pk-cancel")
    job = cl.post(f"{AB}/apps/e2e-pk-cancel/package").json()["job_id"]
    assert cl.delete(f"{AB}/apps/e2e-pk-cancel/package/{job}").status_code == 204
    assert cl.get(f"{AB}/apps/e2e-pk-cancel/package/{job}/progress").status_code == 404


def test_package_of_an_unknown_or_broken_app(pkg: tuple, new_app: Callable) -> None:
    _ob, cl, _ws = pkg
    new_app("e2e-pk-broken", yaml="id: [unclosed\n")
    r = cl.post(f"{AB}/apps/e2e-never-existed/package")
    assert r.status_code == 404 and r.json()["code"] == "app_builder.app_not_found"
    r = cl.post(f"{AB}/apps/e2e-pk-broken/package")
    assert r.status_code == 400 and r.json()["code"] == "app_builder.app_invalid"


# --------------------------------------------------------------------------
# what goes into the zip
# --------------------------------------------------------------------------


def test_the_zip_holds_the_app_and_the_generated_files(pkg: tuple, new_app: Callable) -> None:
    _ob, cl, _ws = pkg
    new_app("e2e-pk-content", files={"frontend/index.html": "<h1>x</h1>", "README.md": "hello", "requirements.txt": "fastapi\nuvicorn\n", "vendor/wheel.txt": "w"})
    _job, fr = _package(cl, "e2e-pk-content")
    names = set(_zip(_done(fr)).namelist())
    assert {"app.yaml", "README.md", "backend/__init__.py", "backend/main.py", "frontend/index.html", "vendor/wheel.txt",
            "run.bat", "requirements.txt", "RUNNING.md", "package_manifest.json"} <= names
    assert {"shared/runner_protocol.py", "shared/telemetry.py", "shared/audio_io.py", "shared/weight_downloader.py"} <= names
    assert not [n for n in names if n.startswith("/") or ".." in n or "\\" in n], "member names are clean relative POSIX paths"


@pytest.mark.parametrize(
    "junk",
    [".venv/lib/x.py", "venv/y.py", "backend/.venv/e.py", "backend/__pycache__/z.cpython-313.pyc", "frontend/node_modules/m.js", "backend/uploads/u.png",
     "backend/package/old.zip", "backend/logs/a.log", "backend/.git/HEAD", "backend/.pytest_cache/c", "backend/.mypy_cache/c", "backend/.ruff_cache/c",
     "backend/c.pyc", "backend/d.pyd", "backend/e.pyo", "backend/VENV/upper.py", "backend/Node_Modules/x.js"],
)
def test_caches_environments_uploads_and_logs_are_left_out(pkg: tuple, new_app: Callable, junk: str) -> None:
    _ob, cl, _ws = pkg
    new_app(f"e2e-pk-junk", files={junk: "junk", "backend/keep.py": "x = 1\n"})
    _job, fr = _package(cl, "e2e-pk-junk")
    names = set(_zip(_done(fr)).namelist())
    assert "backend/keep.py" in names and junk not in names and not [n for n in names if n.lower().endswith((".pyc", ".pyd", ".pyo"))]


def test_files_outside_the_known_folders_are_silently_dropped(pkg: tuple, new_app: Callable) -> None:
    """KNOWN GAP / DESIGN QUESTION (current behaviour, pinned) - APP-ISS-08: only ``backend/``, ``frontend/``, ``vendor/`` and six root files
    are packaged. ``data/``, ``outputs/``, ``tests/``, ``static/``, ``config.json`` ... are left out with no warning."""
    _ob, cl, _ws = pkg
    new_app("e2e-pk-drop", files={"data/seed.json": "{}", "outputs/r.txt": "r", "static/a.css": "a", "tests/t.py": "t", "config.json": "{}", "src/other.py": "o"})
    _job, fr = _package(cl, "e2e-pk-drop")
    z = _zip(_done(fr))
    names = set(z.namelist())
    assert not [n for n in names if n.split("/")[0] in ("data", "outputs", "static", "tests", "src") or n == "config.json"]
    assert json.loads(z.read("package_manifest.json"))["warnings"] == [], "and the manifest does not mention them"


def test_the_manifest_describes_the_package(pkg: tuple, new_app: Callable) -> None:
    _ob, cl, _ws = pkg
    new_app("e2e-pk-manifest", yaml="id: e2e-pk-manifest\nname: Manifest App\ndescription: about it\npackage:\n  include_models: false\n  include_outputs: true\n")
    _job, fr = _package(cl, "e2e-pk-manifest")
    m = json.loads(_zip(_done(fr)).read("package_manifest.json"))
    assert set(m) == {"schema_version", "app_id", "name", "description", "packaged_at", "models", "include_models", "include_outputs", "total_size_bytes",
                      "target_platform", "warnings"}
    assert (m["schema_version"], m["app_id"], m["name"], m["description"], m["models"]) == (1, "e2e-pk-manifest", "Manifest App", "about it", [])
    assert (m["include_models"], m["include_outputs"], m["warnings"]) == (False, True, [])
    assert m["packaged_at"].endswith("+00:00") and abs(time.time() - __import__("datetime").datetime.fromisoformat(m["packaged_at"]).timestamp()) < 300
    assert "Windows" in m["target_platform"] and m["total_size_bytes"] > 0


def test_include_models_false_skips_the_shared_helpers_and_the_models(pkg: tuple, new_app: Callable) -> None:
    _ob, cl, _ws = pkg
    new_app("e2e-pk-nomodels", yaml="id: e2e-pk-nomodels\npackage:\n  include_models: false\n")
    _job, fr = _package(cl, "e2e-pk-nomodels")
    names = _zip(_done(fr)).namelist()
    assert not [n for n in names if n.startswith("shared/")] and "backend/main.py" in names


def test_a_missing_model_becomes_a_manifest_warning_not_a_failure(pkg: tuple, new_app: Callable) -> None:
    _ob, cl, _ws = pkg
    new_app("e2e-pk-missing", yaml="id: e2e-pk-missing\nmodels:\n  - id: not-installed\n    title: Nope\n    model_dir: '${APP_PROJECT_ROOT}/weights'\n")
    _job, fr = _package(cl, "e2e-pk-missing")
    m = json.loads(_zip(_done(fr)).read("package_manifest.json"))
    assert m["models"] == ["not-installed"] and m["warnings"], "packaging went on and recorded why"
    assert any("not-installed" in w or "weights" in w for w in m["warnings"])


def test_the_requirements_file_gets_the_sdk_pin_appended(pkg: tuple, new_app: Callable) -> None:
    _ob, cl, _ws = pkg
    new_app("e2e-pk-req", files={"requirements.txt": "fastapi\nuvicorn\n"})
    _job, fr = _package(cl, "e2e-pk-req")
    lines = _zip(_done(fr)).read("requirements.txt").decode().splitlines()
    assert lines[:2] == ["fastapi", "uvicorn"] and lines[2].startswith("qai-appbuilder>=") and len(lines) == 3


def test_a_requirements_file_that_already_pins_the_sdk_is_kept_as_it_is(pkg: tuple, new_app: Callable) -> None:
    _ob, cl, _ws = pkg
    new_app("e2e-pk-req2", files={"requirements.txt": "fastapi\nqai-appbuilder==2.40.0\n"})
    _job, fr = _package(cl, "e2e-pk-req2")
    assert _zip(_done(fr)).read("requirements.txt").decode() == "fastapi\nqai-appbuilder==2.40.0\n"


def test_an_app_without_requirements_still_gets_one(pkg: tuple, new_app: Callable) -> None:
    _ob, cl, _ws = pkg
    new_app("e2e-pk-req3")
    _job, fr = _package(cl, "e2e-pk-req3")
    text = _zip(_done(fr)).read("requirements.txt").decode()
    assert text.startswith("qai-appbuilder") and text.endswith("\n") and text.count("\n") == 1


def test_run_bat_uses_the_apps_host_and_preferred_port(pkg: tuple, new_app: Callable) -> None:
    _ob, cl, _ws = pkg
    new_app("e2e-pk-bat", yaml="id: e2e-pk-bat\nruntime:\n  host: 127.0.0.1\n  preferred_port: 18777\n")
    new_app("e2e-pk-bat2")
    bat = _zip(_done(_package(cl, "e2e-pk-bat")[1])).read("run.bat").decode()
    assert "18777" in bat and "127.0.0.1" in bat and bat.startswith("@echo off") and "uvicorn" in bat
    default = _zip(_done(_package(cl, "e2e-pk-bat2")[1])).read("run.bat").decode()
    assert "8000" in default and "18777" not in default, "no preferred port means 8000"


def test_an_apps_own_run_bat_is_replaced_by_the_generated_one(pkg: tuple, new_app: Callable) -> None:
    _ob, cl, _ws = pkg
    new_app("e2e-pk-ownbat", files={"run.bat": "@echo MY-OWN-RUN-BAT\r\n"})
    z = _zip(_done(_package(cl, "e2e-pk-ownbat")[1]))
    assert b"MY-OWN-RUN-BAT" not in z.read("run.bat") and z.namelist().count("run.bat") == 1


def test_running_md_explains_how_to_start(pkg: tuple, new_app: Callable) -> None:
    _ob, cl, _ws = pkg
    new_app("e2e-pk-doc")
    text = _zip(_done(_package(cl, "e2e-pk-doc")[1])).read("RUNNING.md").decode()
    assert text.splitlines()[0] == "# Running this packaged app" and "run.bat" in text and "ARM64" in text


def test_non_ascii_file_names_and_content_survive(pkg: tuple, new_app: Callable) -> None:
    _ob, cl, _ws = pkg
    new_app("e2e-pk-utf8", files={"frontend/页面.html": "<p>你好 ✓</p>", "backend/数据.py": "X = '值'\n"})
    z = _zip(_done(_package(cl, "e2e-pk-utf8")[1]))
    assert z.read("frontend/页面.html").decode("utf-8") == "<p>你好 ✓</p>" and z.read("backend/数据.py").decode("utf-8").strip() == "X = '值'"


def test_an_empty_backend_directory_is_not_packaged_as_a_file(pkg: tuple, new_app: Callable) -> None:
    _ob, cl, _ws = pkg
    d = new_app("e2e-pk-empty")
    (d / "frontend").mkdir(exist_ok=True)
    (d / "frontend" / "empty_dir").mkdir()
    z = _zip(_done(_package(cl, "e2e-pk-empty")[1]))
    assert not [n for n in z.namelist() if n.endswith("/")] and z.testzip() is None


def test_the_zip_is_a_valid_deflate_archive(pkg: tuple, new_app: Callable) -> None:
    _ob, cl, _ws = pkg
    new_app("e2e-pk-valid", files={"frontend/big.txt": "abcdefgh" * 50_000})
    done = _done(_package(cl, "e2e-pk-valid")[1])
    z = _zip(done)
    assert z.testzip() is None
    info = z.getinfo("frontend/big.txt")
    assert info.compress_type == zipfile.ZIP_DEFLATED and info.file_size == 400_000 and info.compress_size < info.file_size / 10


def test_a_junction_that_points_outside_the_app_IS_followed_into_the_zip(pkg: tuple, new_app: Callable) -> None:
    """KNOWN GAP - SECURITY (current behaviour, pinned) - APP-ISS-09: a directory junction inside the app folder that points OUTSIDE it is
    walked like a normal folder, so the target's files end up in the zip under ``backend/outside/...``. A junction needs no privilege on
    Windows. In practice the app folder is written by the chat agent, which can be told to create one; the zip is then shared. If the
    packager is changed to skip reparse points this assertion flips - update it then.

    The test removes the junction with ``os.rmdir`` (which never touches the target's content) and its target is a temp folder anyway."""
    import subprocess

    _ob, cl, ws = pkg
    outside = ws / "outside-the-app"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOP-SECRET-OUTSIDE-THE-APP")
    d = new_app("e2e-pk-link")
    link = d / "backend" / "outside"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, text=True)
    assert r.returncode == 0 and (link / "secret.txt").exists(), r.stdout + r.stderr
    try:
        z = _zip(_done(_package(cl, "e2e-pk-link")[1]))
        assert z.read("backend/outside/secret.txt") == b"TOP-SECRET-OUTSIDE-THE-APP"
        assert "backend/main.py" in z.namelist()
    finally:
        os.rmdir(link)
        assert (outside / "secret.txt").read_text() == "TOP-SECRET-OUTSIDE-THE-APP", "removing the junction did not touch its target"

# --------------------------------------------------------------------------
# repeated packaging
# --------------------------------------------------------------------------


def test_two_packages_in_the_same_second_overwrite_each_other(pkg: tuple, new_app: Callable) -> None:
    """KNOWN GAP (current behaviour, pinned) - APP-ISS-06: the name has one-second resolution, so a second package made in the same second
    silently replaces the first (the earlier ``zip_path`` then points at the newer content)."""
    _ob, cl, ws = pkg
    new_app("e2e-pk-same", files={"README.md": "first"})
    d1 = _done(_package(cl, "e2e-pk-same")[1])
    (_ob.data_dir / "app_builder" / "e2e-pk-same" / "README.md").write_text("second")
    d2 = _done(_package(cl, "e2e-pk-same")[1])
    if d1["zip_path"] == d2["zip_path"]:
        assert _zip(d2).read("README.md") == b"second"
    else:
        pytest.skip("the two packages landed in different seconds; the clash needs the same second")


def test_packages_made_at_different_times_coexist(pkg: tuple, new_app: Callable) -> None:
    _ob, cl, ws = pkg
    new_app("e2e-pk-two")
    a = _done(_package(cl, "e2e-pk-two")[1])
    time.sleep(1.2)
    b = _done(_package(cl, "e2e-pk-two")[1])
    assert a["zip_path"] != b["zip_path"] and Path(a["zip_path"]).is_file() and Path(b["zip_path"]).is_file()


def test_deleting_the_app_leaves_its_packages(pkg: tuple, new_app: Callable) -> None:
    _ob, cl, _ws = pkg
    new_app("e2e-pk-keep")
    z = Path(_done(_package(cl, "e2e-pk-keep")[1])["zip_path"])
    assert cl.delete(f"{AB}/apps/e2e-pk-keep").status_code == 204
    assert z.is_file()


def test_packaging_does_not_change_the_app_folder(pkg: tuple, new_app: Callable) -> None:
    _ob, cl, _ws = pkg
    d = new_app("e2e-pk-readonly", files={"frontend/a.html": "a"})
    before = sorted((str(p.relative_to(d)), p.stat().st_size) for p in d.rglob("*") if p.is_file())
    _package(cl, "e2e-pk-readonly")
    assert sorted((str(p.relative_to(d)), p.stat().st_size) for p in d.rglob("*") if p.is_file()) == before


def test_concurrent_packages_of_different_apps_both_finish(pkg: tuple, new_app: Callable) -> None:
    _ob, cl, _ws = pkg
    new_app("e2e-pk-c1")
    new_app("e2e-pk-c2")
    j1 = cl.post(f"{AB}/apps/e2e-pk-c1/package").json()["job_id"]
    j2 = cl.post(f"{AB}/apps/e2e-pk-c2/package").json()["job_id"]
    assert j1 != j2
    d2 = _done(_frames(cl, "e2e-pk-c2", j2))
    d1 = _done(_frames(cl, "e2e-pk-c1", j1))
    assert Path(d1["zip_path"]).name.startswith("e2e-pk-c1-") and Path(d2["zip_path"]).name.startswith("e2e-pk-c2-")


# --------------------------------------------------------------------------
# the workspace override
# --------------------------------------------------------------------------


def test_with_no_workspace_override_the_zip_would_go_to_the_real_work_folder() -> None:
    """Documents WHY the module overrides the workspace: the default ``QAI_WORKSPACE__MODEL_ROOT`` is ``C:/WoS_AI``. This test only reads
    the setting through a throw-away backend's config summary-equivalent route; it packages nothing."""
    from tests.e2e import conftest as c

    text = (c.REPO_ROOT / "apps" / "api" / "_workspace_resolver.py").read_text(encoding="utf-8")
    assert 'DEFAULT_WORKSPACE_ROOT: str = (\n    "C:/WoS_AI"' in text
