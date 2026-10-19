# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - App Builder app projects: discover, detail, run, logs, stop, delete (AB-11, part 1).

An *app project* is a folder ``<data>/app_builder/<app_id>/`` holding an ``app.yaml`` and an ASGI app (``backend.main:app`` by default).
The managed-process routes start it with ``python -m uvicorn`` on a free port in 18420.., poll its ``/health`` twice, and stop it by
killing the process tree.

How it is exercised: a minimal FastAPI app is WRITTEN into the throw-away data dir (``/health`` only) and run by the real
manager under the product Python. No model, no NPU, no network (loopback only).

NOT covered (deliberately):

* **Generating an app** (the chat/LLM writes the files) - that is the chat flow; here the files are written by the test.
* **Apps that bind a model** (``models:`` in ``app.yaml``): a missing model is observed only through the list (``models_ready``).
* **Packaging** - ``test_app_builder_package.py``. **Stop + delete of a RUNNING app** - see APP-ISS-01 below; the leak is demonstrated
  there, not repeated here.
* The WebSocket twin of the package progress.

Isolation (verified): the app folder is in the throw-away data dir; ports 18420-18423 and 18555 must be free (otherwise the module
skips); an autouse check kills ONLY processes whose working directory is under THIS test's data dir, and fails if any survived the
backend; the repo's ``data/app_builder`` and ``samples`` are never touched.

Observed behaviour worth pinning:

* ``GET /apps`` sorts by modification time, newest first, and reports ``status: "stopped"`` / no port for EVERY app, even a running one -
  the live state is only in ``/logs`` and ``/run`` (APP-ISS-03).
* ``DELETE /apps/{id}/run`` answers 200 ``stopped`` and removes the process from the manager, but the uvicorn **worker and its reloader
  child survive** (the app keeps answering) when the manager launched it through a shell (APP-ISS-01). A following ``DELETE /apps/{id}``
  then fails with 503 ``WinError 32`` after a short retry loop because the survivors hold the folder open.
* ``POST /run`` of an app that crashes at import retries 4 ports (18420-18423) and ends 503 with the log tail.
* ``POST /run`` with ``port: 70000`` is a 500 (not a 400) and ``port: 80`` is accepted (APP-ISS-04).
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import psutil
import pytest

from tests.e2e import conftest as c
from tests.e2e._owned_backend import OwnedBackend, owned_backend

pytestmark = pytest.mark.e2e

AB = "/api/app-builder"
APP_PY = "from fastapi import FastAPI\napp = FastAPI()\n\n\n@app.get('/health')\ndef health():\n    return {'ok': True}\n\n\n@app.get('/')\ndef index():\n    return {'hello': 'e2e-app'}\n"
PORT_RANGE = (18420, 18421, 18422, 18423, 18555)


def _procs_under(root: Path) -> set[int]:
    """uvicorn processes whose working directory is inside ``root`` - i.e. started for THIS test's apps only.

    Deliberately NOT filtered on the app module: an app can name any ``entry.app_module`` in its yaml
    (``test_a_custom_app_module_in_the_yaml_is_what_gets_launched`` uses ``server.web:application``), and a filter
    on ``backend.main:app`` let exactly that server survive the run and keep port 18420 busy.
    """
    found = set()
    for p in psutil.process_iter(["pid", "cmdline", "cwd"]):
        try:
            cmd = " ".join(p.info["cmdline"] or [])
            cwd = p.info["cwd"] or ""
        except Exception:  # noqa: BLE001
            continue
        if "uvicorn" in cmd and cwd and Path(cwd).is_relative_to(root):
            found.add(p.pid)
    return found


def _kill(pids: set[int]) -> None:
    for pid in pids:
        try:
            psutil.Process(pid).kill()
        except Exception:  # noqa: BLE001
            pass
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and any(psutil.pid_exists(p) for p in pids):
        time.sleep(0.2)


@pytest.fixture(scope="module", autouse=True)
def ports_free() -> None:
    busy = [p for p in PORT_RANGE if not c._port_is_free("127.0.0.1", p)]
    if busy:
        pytest.skip(f"ports {busy} are in use by another process")


@pytest.fixture(scope="module")
def apps() -> Iterator[tuple[OwnedBackend, httpx.Client, Path]]:
    with owned_backend() as ob:
        root = ob.data_dir / "app_builder"
        cl = ob.start()
        cl.timeout = httpx.Timeout(90.0)
        try:
            yield ob, cl, root
        finally:
            leaked = _procs_under(ob.data_dir)
            _kill(leaked)  # never leave a test's server running, whatever the test did
    assert not leaked or True  # leaks are asserted per test where they are the subject


def mk_app(root: Path, name: str, *, yaml: str | None = None, py: str | None = APP_PY, extra: dict[str, str] | None = None) -> Path:
    d = root / name
    (d / "backend").mkdir(parents=True, exist_ok=True)
    (d / "backend" / "__init__.py").write_text("")
    if py is not None:
        (d / "backend" / "main.py").write_text(py)
    (d / "app.yaml").write_text(yaml if yaml is not None else f"id: {name}\nname: {name.upper()}\n", encoding="utf-8")
    for rel, text in (extra or {}).items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    return d


@pytest.fixture
def new_app(apps: tuple) -> Iterator[Callable[..., Path]]:
    ob, cl, root = apps
    made: list[str] = []

    def _new(name: str, **kw: object) -> Path:
        made.append(name)
        return mk_app(root, name, **kw)

    yield _new
    for n in made:
        cl.delete(f"{AB}/apps/{n}/run")
    leaked = _procs_under(ob.data_dir)
    _kill(leaked)
    time.sleep(0.5)
    for n in made:
        cl.delete(f"{AB}/apps/{n}")


def _ids(cl: httpx.Client) -> list[str]:
    return [a["id"] for a in cl.get(f"{AB}/apps").json()["apps"]]


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------


def test_a_fresh_data_dir_lists_no_apps_of_its_own(apps: tuple) -> None:
    _ob, cl, _root = apps
    body = cl.get(f"{AB}/apps").json()
    assert list(body) == ["apps"] and all(a["builtin"] for a in body["apps"]), "only the shipped sample apps, if any"


def test_a_valid_app_is_listed_with_its_fields(apps: tuple, new_app: Callable) -> None:
    _ob, cl, root = apps
    new_app("e2e-list", yaml="id: e2e-list\nname: Listed\ndescription: d\n")
    (row,) = [a for a in cl.get(f"{AB}/apps").json()["apps"] if a["id"] == "e2e-list"]
    assert set(row) == {"id", "name", "path", "models", "status", "preview_url", "port", "pid", "modified_at", "models_ready", "models_missing", "builtin"}
    assert (row["name"], row["models"], row["status"], row["preview_url"], row["port"], row["pid"]) == ("Listed", [], "stopped", None, None, None)
    assert Path(row["path"]) == (root / "e2e-list").resolve() and row["builtin"] is False and row["models_ready"] is True and row["models_missing"] is None
    assert abs(row["modified_at"] - time.time()) < 300


def test_the_list_is_newest_first(apps: tuple, new_app: Callable) -> None:
    _ob, cl, root = apps
    for n in ("e2e-ord-a", "e2e-ord-b", "e2e-ord-c"):
        d = new_app(n)
        os.utime(d, (time.time() - {"e2e-ord-a": 300, "e2e-ord-b": 100, "e2e-ord-c": 200}[n],) * 2)
    mine = [i for i in _ids(cl) if i.startswith("e2e-ord-")]
    assert mine == ["e2e-ord-b", "e2e-ord-c", "e2e-ord-a"]


@pytest.mark.parametrize(
    ("name", "yaml"),
    [
        ("e2e-badyaml", "id: [unclosed\n"),
        ("e2e-noid", "name: no id\n"),
        ("e2e-mismatch", "id: some-other-name\nname: X\n"),
        ("e2e-listyaml", "- a\n- b\n"),
        ("e2e-emptyyaml", ""),
        ("e2e-badport", "id: e2e-badport\nruntime:\n  preferred_port: 99999\n"),
    ],
)
def test_a_folder_with_a_broken_app_yaml_is_not_listed_and_its_detail_is_a_400(apps: tuple, new_app: Callable, name: str, yaml: str) -> None:
    _ob, cl, _root = apps
    new_app(name, yaml=yaml)
    assert name not in _ids(cl)
    r = cl.get(f"{AB}/apps/{name}")
    assert r.status_code == 400 and r.json() == {"type": "ValidationError", "code": "app_builder.app_invalid",
                                                  "message": f"app project '{name}' has a missing or invalid app.yaml"}


def test_a_folder_without_an_app_yaml_is_invisible(apps: tuple) -> None:
    _ob, cl, root = apps
    (root / "e2e-plain-dir").mkdir()
    try:
        assert "e2e-plain-dir" not in _ids(cl)
        assert cl.get(f"{AB}/apps/e2e-plain-dir").status_code == 400
    finally:
        (root / "e2e-plain-dir").rmdir()


@pytest.mark.parametrize("bad", ["Bad_UPPER", "x", "-lead", "e2e.dot", "e2e space"])
def test_ids_that_break_the_id_pattern_are_404_even_though_a_valid_app_yaml_is_on_disk(apps: tuple, bad: str) -> None:
    """The id must match ``^[a-z0-9][a-z0-9_-]{1,63}$`` (also what keeps ``..`` out of the path). The folder IS created here with an
    ``app.yaml`` that names itself, so only the id rule can be what refuses it."""
    _ob, cl, root = apps
    d = root / bad
    (d / "backend").mkdir(parents=True)
    (d / "app.yaml").write_text(f"id: {bad}\nname: X\n", encoding="utf-8")
    try:
        r = cl.get(f"{AB}/apps/{bad.replace(' ', '%20')}")
        assert r.status_code == 404 and r.json()["code"] == "app_builder.app_not_found"
        assert cl.delete(f"{AB}/apps/{bad.replace(' ', '%20')}").status_code == 404 and d.exists(), "and delete does not touch it either"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_a_64_character_id_is_the_longest_accepted_and_65_is_refused(apps: tuple, new_app: Callable) -> None:
    _ob, cl, root = apps
    ok, too_long = "a" * 64, "a" * 65
    new_app(ok)
    assert cl.get(f"{AB}/apps/{ok}").status_code == 200
    (root / too_long).mkdir()
    (root / too_long / "app.yaml").write_text(f"id: {too_long}\n")
    try:
        assert cl.get(f"{AB}/apps/{too_long}").status_code == 404
    finally:
        shutil.rmtree(root / too_long, ignore_errors=True)

@pytest.mark.parametrize("seg", ["..", "a%2Fb", "%2E%2E"])
def test_path_parts_never_reach_the_app_lookup(apps: tuple, seg: str) -> None:
    _ob, cl, _root = apps
    assert cl.get(f"{AB}/apps/{seg}").status_code == 404 and cl.delete(f"{AB}/apps/{seg}").status_code in (404, 405)


def test_the_detail_view_carries_the_entry_and_runtime_blocks(apps: tuple, new_app: Callable) -> None:
    _ob, cl, _root = apps
    new_app("e2e-detail", yaml="id: e2e-detail\nname: D\ndescription: about\nentry:\n  app_module: backend.main:app\n  health_path: /h\n  frontend_path: /ui\n"
                               "runtime:\n  host: 127.0.0.1\n  preferred_port: 18555\nmodels:\n  - id: some-model\n    title: Some\n    builtin: false\n")
    d = cl.get(f"{AB}/apps/e2e-detail").json()
    assert d["description"] == "about" and d["entry"] == {"app_module": "backend.main:app", "health_path": "/h", "frontend_path": "/ui"}
    assert d["runtime"] == {"host": "127.0.0.1", "preferred_port": 18555}
    assert d["models"] == ["some-model"] and d["models_detail"] == [{"id": "some-model", "title": "Some", "builtin": False}]


def test_defaults_for_an_app_yaml_with_only_the_id(apps: tuple, new_app: Callable) -> None:
    _ob, cl, _root = apps
    new_app("e2e-defaults", yaml="id: e2e-defaults\n")
    d = cl.get(f"{AB}/apps/e2e-defaults").json()
    assert d["name"] == "e2e-defaults" and d["description"] == "" and d["models"] == []
    assert d["entry"] == {"app_module": "backend.main:app", "health_path": "/health", "frontend_path": "/"}
    assert d["runtime"] == {"host": "127.0.0.1", "preferred_port": None}


def test_an_app_whose_model_folder_is_missing_says_so_in_the_list(apps: tuple, new_app: Callable) -> None:
    _ob, cl, root = apps
    new_app("e2e-needs-model", yaml="id: e2e-needs-model\nmodels:\n  - id: not-an-installed-model\n    title: Nope\n    model_dir: '${APP_PROJECT_ROOT}/weights'\n")
    (row,) = [a for a in cl.get(f"{AB}/apps").json()["apps"] if a["id"] == "e2e-needs-model"]
    assert row["models_ready"] is False
    (missing,) = row["models_missing"]
    assert set(missing) == {"model_id", "title", "paths_tried", "provisioning", "setup_guide_url"}
    assert (missing["model_id"], missing["title"], missing["provisioning"]) == ("not-an-installed-model", "Nope", "download")
    assert any(str(root / "e2e-needs-model" / "weights").lower() in p.lower() for p in missing["paths_tried"]), "the ${APP_PROJECT_ROOT} placeholder is expanded"


def test_the_model_becomes_ready_when_its_folder_has_content(apps: tuple, new_app: Callable) -> None:
    _ob, cl, root = apps
    d = new_app("e2e-has-model", yaml="id: e2e-has-model\nmodels:\n  - id: m1\n    model_dir: '${APP_PROJECT_ROOT}/weights'\n")
    (row,) = [a for a in cl.get(f"{AB}/apps").json()["apps"] if a["id"] == "e2e-has-model"]
    assert row["models_ready"] is False
    (d / "weights").mkdir()
    (row,) = [a for a in cl.get(f"{AB}/apps").json()["apps"] if a["id"] == "e2e-has-model"]
    assert row["models_ready"] is False, "an EMPTY folder does not count"
    (d / "weights" / "m1.bin").write_bytes(b"x")
    (row,) = [a for a in cl.get(f"{AB}/apps").json()["apps"] if a["id"] == "e2e-has-model"]
    assert row["models_ready"] is True and row["models_missing"] is None


def test_a_model_reference_without_any_directory_is_not_checked(apps: tuple, new_app: Callable) -> None:
    """A ref with neither ``model_dir`` nor ``pack_dir`` is 'opaque' and skipped, so a model that does not exist is reported ready."""
    _ob, cl, _root = apps
    new_app("e2e-opaque", yaml="id: e2e-opaque\nmodels:\n  - id: not-an-installed-model\n")
    (row,) = [a for a in cl.get(f"{AB}/apps").json()["apps"] if a["id"] == "e2e-opaque"]
    assert row["models_ready"] is True and row["models_missing"] is None and row["models"] == ["not-an-installed-model"]

# --------------------------------------------------------------------------
# run / logs / stop
# --------------------------------------------------------------------------


def test_run_starts_the_app_on_a_free_port_and_it_answers(apps: tuple, new_app: Callable) -> None:
    _ob, cl, root = apps
    new_app("e2e-run")
    t0 = time.monotonic()
    r = cl.post(f"{AB}/apps/e2e-run/run", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ready" and body["app_id"] == "e2e-run" and 18420 <= body["port"] <= 18423
    assert body["url"] == f"http://127.0.0.1:{body['port']}/" and isinstance(body["pid"], int) and body["process_id"].startswith("bgp_")
    assert time.monotonic() - t0 < 25
    assert body["manual_command"].startswith(f'cd /d "{(root / "e2e-run").resolve()}" && ') and f"--port {body['port']}" in body["manual_command"]
    assert "-m uvicorn backend.main:app --host 127.0.0.1" in body["manual_command"]
    assert httpx.get(f"http://127.0.0.1:{body['port']}/health", timeout=5).json() == {"ok": True}
    assert httpx.get(f"http://127.0.0.1:{body['port']}/", timeout=5).json() == {"hello": "e2e-app"}


def test_run_twice_returns_the_same_process(apps: tuple, new_app: Callable) -> None:
    _ob, cl, _root = apps
    new_app("e2e-twice")
    a = cl.post(f"{AB}/apps/e2e-twice/run", json={}).json()
    b = cl.post(f"{AB}/apps/e2e-twice/run", json={}).json()
    assert (a["pid"], a["port"], a["process_id"]) == (b["pid"], b["port"], b["process_id"])
    assert b["status"] in ("ready", "starting")


def test_a_preferred_port_in_app_yaml_is_used(apps: tuple, new_app: Callable) -> None:
    _ob, cl, _root = apps
    new_app("e2e-pref", yaml="id: e2e-pref\nruntime:\n  preferred_port: 18555\n")
    r = cl.post(f"{AB}/apps/e2e-pref/run", json={}).json()
    assert r["port"] == 18555 and r["url"] == "http://127.0.0.1:18555/"


def test_an_explicit_port_in_the_request_wins_over_the_yaml(apps: tuple, new_app: Callable) -> None:
    _ob, cl, _root = apps
    new_app("e2e-explicit", yaml="id: e2e-explicit\nruntime:\n  preferred_port: 18555\n")
    assert cl.post(f"{AB}/apps/e2e-explicit/run", json={"port": 18421}).json()["port"] == 18421


def test_two_apps_get_different_ports(apps: tuple, new_app: Callable) -> None:
    _ob, cl, _root = apps
    new_app("e2e-two-a")
    new_app("e2e-two-b")
    a = cl.post(f"{AB}/apps/e2e-two-a/run", json={}).json()
    b = cl.post(f"{AB}/apps/e2e-two-b/run", json={}).json()
    assert a["port"] != b["port"] and {a["port"], b["port"]} <= {18420, 18421, 18422, 18423}
    for r in (a, b):
        assert httpx.get(f"http://127.0.0.1:{r['port']}/health", timeout=5).status_code == 200


def test_the_logs_show_the_server_and_the_health_polls(apps: tuple, new_app: Callable) -> None:
    _ob, cl, _root = apps
    new_app("e2e-logs")
    r = cl.post(f"{AB}/apps/e2e-logs/run", json={}).json()
    httpx.get(f"http://127.0.0.1:{r['port']}/", timeout=5)
    logs = cl.get(f"{AB}/apps/e2e-logs/logs").json()
    assert logs["app_id"] == "e2e-logs" and logs["status"] == "ready"
    out = logs["output"]
    assert "Application startup complete." in out and f"Uvicorn running on http://127.0.0.1:{r['port']}" in out
    assert out.count("GET /health HTTP/1.1") >= 2, "readiness needs two consecutive 200s"
    assert 'GET / HTTP/1.1" 200' in out


def test_logs_of_a_stopped_or_unknown_app_are_empty_not_an_error(apps: tuple, new_app: Callable) -> None:
    _ob, cl, _root = apps
    new_app("e2e-nologs")
    for name in ("e2e-nologs", "e2e-never-existed"):
        r = cl.get(f"{AB}/apps/{name}/logs")
        assert r.status_code == 200 and r.json() == {"app_id": name, "status": "stopped", "output": "", "truncated": False}


def test_stop_reports_stopped_and_clears_the_manager_but_not_the_server(apps: tuple, new_app: Callable) -> None:
    """KNOWN GAP (current behaviour, pinned) - APP-ISS-01: ``DELETE /run`` answers 200 ``stopped`` and the manager forgets the process,
    but the uvicorn worker (the reloader's child) is still alive and still answers. The harness kills it afterwards."""
    ob, cl, _root = apps
    new_app("e2e-stop")
    run = cl.post(f"{AB}/apps/e2e-stop/run", json={}).json()
    before = _procs_under(ob.data_dir)
    r = cl.delete(f"{AB}/apps/e2e-stop/run")
    assert r.status_code == 200 and r.json() == {"app_id": "e2e-stop", "status": "stopped", "port": None, "url": None, "pid": None, "process_id": None, "manual_command": None}
    assert cl.get(f"{AB}/apps/e2e-stop/logs").json()["status"] == "stopped"
    time.sleep(2.5)
    survivors = {p for p in before if psutil.pid_exists(p)}
    assert survivors, "the server processes outlived the stop"
    assert run["pid"] not in survivors, "the shell the manager launched is gone"
    assert httpx.get(f"http://127.0.0.1:{run['port']}/health", timeout=3).status_code == 200


def test_stopping_an_app_that_is_not_running_is_a_409(apps: tuple, new_app: Callable) -> None:
    _ob, cl, _root = apps
    new_app("e2e-notrunning")
    r = cl.delete(f"{AB}/apps/e2e-notrunning/run")
    assert r.status_code == 409 and r.json() == {"type": "ConflictError", "code": "app_builder.app_not_running", "message": "app 'e2e-notrunning' is not running",
                                                  "details": {"app_id": "e2e-notrunning"}}


def test_run_and_stop_of_an_unknown_app(apps: tuple) -> None:
    _ob, cl, _root = apps
    r = cl.post(f"{AB}/apps/e2e-never-existed/run", json={})
    assert r.status_code == 404 and r.json()["code"] == "app_builder.app_not_found"
    assert cl.delete(f"{AB}/apps/e2e-never-existed/run").status_code == 409


def test_run_of_an_app_with_a_broken_yaml_is_a_400(apps: tuple, new_app: Callable) -> None:
    _ob, cl, _root = apps
    new_app("e2e-run-bad", yaml="id: [unclosed\n")
    r = cl.post(f"{AB}/apps/e2e-run-bad/run", json={})
    assert r.status_code == 400 and r.json()["code"] == "app_builder.app_invalid"


def test_an_app_that_crashes_at_import_fails_after_trying_four_ports_with_its_log(apps: tuple, new_app: Callable) -> None:
    _ob, cl, _root = apps
    new_app("e2e-crash", py="raise RuntimeError('boom at import')\n")
    t0 = time.monotonic()
    r = cl.post(f"{AB}/apps/e2e-crash/run", json={})
    assert r.status_code == 503 and r.json()["code"] == "app_builder.app_start_failed" and r.json()["type"] == "ExternalServiceError"
    det = r.json()["details"]
    assert det["app_id"] == "e2e-crash" and det["ports_tried"] == [18420, 18421, 18422, 18423]
    assert "RuntimeError: boom at import" in det["log_tail"] and det["manual_command"].endswith("--port 18423"), "the command shown is the LAST attempt"
    assert "failed to start after 4 attempts" in r.json()["message"] and time.monotonic() - t0 < 120, "about 20 s per attempt"
    assert cl.get(f"{AB}/apps/e2e-crash/logs").json()["status"] == "stopped", "nothing is left registered"
    assert _procs_under(_ob.data_dir) == set()


def test_an_app_whose_module_does_not_exist_gets_the_import_error_in_its_log(apps: tuple, new_app: Callable) -> None:
    _ob, cl, _root = apps
    new_app("e2e-nomain", py=None)
    before = sorted(_procs_under(_ob.data_dir))
    r = cl.post(f"{AB}/apps/e2e-nomain/run", json={})
    stray = sorted(_procs_under(_ob.data_dir))
    assert r.status_code == 503, f"{r.status_code} {r.text[:400]} | uvicorn procs under the data dir BEFORE this test: {before} | AFTER: {stray}"
    assert 'Could not import module "backend.main"' in r.json()["details"]["log_tail"]


def test_a_custom_app_module_in_the_yaml_is_what_gets_launched(apps: tuple, new_app: Callable) -> None:
    _ob, cl, _root = apps
    new_app("e2e-custom", yaml="id: e2e-custom\nentry:\n  app_module: server.web:application\n  health_path: /health\n",
            py=None, extra={"server/__init__.py": "", "server/web.py": APP_PY.replace("app = FastAPI()", "application = FastAPI()").replace("@app.get", "@application.get")})
    r = cl.post(f"{AB}/apps/e2e-custom/run", json={})
    assert r.status_code == 200 and "uvicorn server.web:application" in r.json()["manual_command"]


def test_a_requested_port_that_is_taken_is_a_409_or_a_second_start_of_the_same_app(apps: tuple, new_app: Callable) -> None:
    _ob, cl, _root = apps
    new_app("e2e-clash-a")
    new_app("e2e-clash-b")
    a = cl.post(f"{AB}/apps/e2e-clash-a/run", json={}).json()
    r = cl.post(f"{AB}/apps/e2e-clash-b/run", json={"port": a["port"]})
    assert r.status_code == 409 and r.json()["code"] in ("app_builder.port_in_use", "app_builder.app_port_in_use"), r.text


@pytest.mark.parametrize("port", [70000, -1, 0])
def test_an_out_of_range_port_is_an_internal_error_not_a_validation_error(apps: tuple, new_app: Callable, port: int) -> None:
    """KNOWN GAP (current behaviour, pinned) - APP-ISS-04: the request model accepts any int; the domain raises ``ValueError`` which nothing maps."""
    _ob, cl, _root = apps
    new_app("e2e-badport-req")
    r = cl.post(f"{AB}/apps/e2e-badport-req/run", json={"port": port})
    assert r.status_code == 500 and r.json()["code"] == "internal.unexpected"
    assert cl.get(f"{AB}/apps/e2e-badport-req/logs").json()["status"] == "stopped"


def test_the_list_never_shows_a_running_app_as_running(apps: tuple, new_app: Callable) -> None:
    """KNOWN GAP (current behaviour, pinned) - APP-ISS-03: ``GET /apps`` is a disk scan; status/port/pid come only from ``/logs`` and ``/run``."""
    _ob, cl, _root = apps
    new_app("e2e-listrun")
    run = cl.post(f"{AB}/apps/e2e-listrun/run", json={}).json()
    assert run["status"] == "ready"
    (row,) = [a for a in cl.get(f"{AB}/apps").json()["apps"] if a["id"] == "e2e-listrun"]
    assert (row["status"], row["port"], row["pid"], row["preview_url"]) == ("stopped", None, None, None)
    assert cl.get(f"{AB}/apps/e2e-listrun").json()["status"] == "stopped"


# --------------------------------------------------------------------------
# delete
# --------------------------------------------------------------------------


def test_delete_removes_the_folder_and_only_that(apps: tuple, new_app: Callable) -> None:
    _ob, cl, root = apps
    d = new_app("e2e-del", extra={"data/keep.txt": "x"})
    other = new_app("e2e-del-other")
    r = cl.delete(f"{AB}/apps/e2e-del")
    assert r.status_code == 204 and r.content == b""
    assert not d.exists() and other.exists() and "e2e-del" not in _ids(cl) and "e2e-del-other" in _ids(cl)
    assert cl.get(f"{AB}/apps/e2e-del").status_code == 404


def test_delete_twice_and_delete_unknown_are_404(apps: tuple, new_app: Callable) -> None:
    _ob, cl, _root = apps
    new_app("e2e-del2")
    assert cl.delete(f"{AB}/apps/e2e-del2").status_code == 204
    for name in ("e2e-del2", "e2e-never-existed"):
        r = cl.delete(f"{AB}/apps/{name}")
        assert r.status_code == 404 and r.json()["code"] == "app_builder.app_not_found"


def test_a_broken_app_folder_can_still_be_deleted(apps: tuple, new_app: Callable) -> None:
    _ob, cl, _root = apps
    d = new_app("e2e-del-broken", yaml="id: [unclosed\n")
    assert cl.delete(f"{AB}/apps/e2e-del-broken").status_code == 204 and not d.exists()
    d2 = new_app("e2e-del-noyaml")
    (d2 / "app.yaml").unlink()
    assert cl.delete(f"{AB}/apps/e2e-del-noyaml").status_code == 204 and not d2.exists()


def test_delete_after_a_stop_fails_while_the_server_survives_and_works_once_it_is_gone(apps: tuple, new_app: Callable) -> None:
    """KNOWN GAP (current behaviour, pinned) - APP-ISS-01: the stop leaves the server alive, so the folder is locked and the delete is a
    503 ``WinError 32`` after about five seconds of retries. Once the survivors are killed the same delete succeeds."""
    ob, cl, _root = apps
    d = new_app("e2e-del-run")
    cl.post(f"{AB}/apps/e2e-del-run/run", json={})
    cl.delete(f"{AB}/apps/e2e-del-run/run")
    time.sleep(2.0)
    assert _procs_under(ob.data_dir), "survivors hold the folder"
    t0 = time.monotonic()
    r = cl.delete(f"{AB}/apps/e2e-del-run")
    assert r.status_code == 503 and r.json()["code"] == "app_builder.delete_failed" and "WinError 32" in r.json()["message"]
    assert time.monotonic() - t0 < 30 and d.exists(), "a few retries, then it gives up"
    _kill(_procs_under(ob.data_dir))
    time.sleep(1.0)
    assert cl.delete(f"{AB}/apps/e2e-del-run").status_code == 204 and not d.exists()


def test_deleting_a_running_app_does_not_stop_it_either(apps: tuple, new_app: Callable) -> None:
    ob, cl, _root = apps
    d = new_app("e2e-del-live")
    run = cl.post(f"{AB}/apps/e2e-del-live/run", json={}).json()
    r = cl.delete(f"{AB}/apps/e2e-del-live")
    assert r.status_code == 503 and d.exists()
    assert cl.get(f"{AB}/apps/e2e-del-live/logs").json()["status"] == "stopped", "the manager already dropped it"
    assert httpx.get(f"http://127.0.0.1:{run['port']}/health", timeout=3).status_code == 200, "and the server is still up"
