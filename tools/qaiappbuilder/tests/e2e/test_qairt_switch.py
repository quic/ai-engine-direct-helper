# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - QAIRT SDK hot-switch API: preflight and the validation edges of create / lookup (MB-07, part 2).

Own module on purpose: ONE module-scoped backend (``switch_backend``) serves every test here, because starting a backend
costs ~15 s. A module-scoped backend holds port 8989 for the whole module, so no test in this file may start another one
(the suite refuses to kill a process it did not start). The tests that need their own backends - one per env-file shape,
no env file, a structurally wrong env file - are in ``test_qairt_env.py`` and ``test_qairt_env_bad_file.py``.

Why sharing is safe: every test here only READS, or sends a request that is rejected before a job exists; the env file is
asserted byte-identical in ``test_preflight_changes_nothing`` and ``test_create_with_an_unknown_target_...``. NOTHING here
sends a valid, acknowledged switch (that would install a wheel and rewrite ``qairt_env.json``).

See ``test_qairt_env.py`` for what is NOT covered and for the isolation guarantees (fake SDK folder, the repo's real
``data/config/qairt_env.json`` hashed before/after).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from tests.e2e import conftest as c
from tests.e2e._owned_backend import OwnedBackend, owned_backend
from tests.e2e.conftest import REPO_ROOT

pytestmark = pytest.mark.e2e

PRE = "/api/system/qairt-switches/preflight"
SW = "/api/system/qairt-switches"
REAL_ENV = REPO_ROOT / "data" / "config" / "qairt_env.json"
VERSION_DIRS = ("2.48.40.260702", "2.49.40.260810", "2.9.1", "2.10.1", "2.3.7", "2.51.40", "2.48.40.260801", "2.48.40")
NOT_VERSIONS = ("2.10", "v2.50.0", "2.50.0-beta", "2.50.0.1.2", "latest", " 2.52.0")


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


@pytest.fixture(scope="module", autouse=True)
def real_env_file_untouched() -> Iterator[None]:
    before = _sha(REAL_ENV)
    yield
    assert _sha(REAL_ENV) == before, "the repo's own data/config/qairt_env.json changed during this module"


@contextmanager
def backend_with_env(payload: object | None) -> Iterator[tuple[OwnedBackend, httpx.Client]]:
    with owned_backend() as ob:
        if payload is not None:
            cfg = ob.data_dir / "config"
            cfg.mkdir()
            (cfg / "qairt_env.json").write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
        cl = ob.start()
        yield ob, cl


def _installed_appbuilder() -> str:
    """The qai_appbuilder wheel version of the interpreter the backend runs under."""
    out = subprocess.run(
        [c._python_executable(), "-W", "ignore", "-c", "import importlib.metadata as m; print(m.version('qai_appbuilder'))"],
        capture_output=True, text=True, timeout=60,
    )
    return out.stdout.strip().splitlines()[-1] if out.returncode == 0 and out.stdout.strip() else ""


def _field_errors(r: httpx.Response) -> dict:
    assert r.status_code == 400, r.text
    return r.json()["details"]["field_errors"]

# --------------------------------------------------------------------------
# preflight (side-effect free)
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def switch_backend() -> Iterator[tuple[OwnedBackend, httpx.Client, Path]]:
    """ONE backend for every preflight / validation test below (starting one costs ~15 s). Safe to share: these tests only
    read, or send requests that are rejected before a job exists, and ``test_preflight_changes_nothing`` /
    ``test_create_*`` assert the env file stays byte-identical. A module-scoped fake SDK keeps it independent of the
    function-scoped ``fake_sdk`` fixture."""
    root = Path(tempfile.mkdtemp(prefix="qai-fake-qairt-"))
    for n in (*VERSION_DIRS, *NOT_VERSIONS):
        (root / n).mkdir()
    try:
        with backend_with_env({"qairt_sdk_root": str(root / "2.48.40.260702"), "schema_version": 2}) as (ob, cl):
            yield ob, cl, ob.data_dir / "config" / "qairt_env.json"
    finally:
        shutil.rmtree(root, ignore_errors=True)

def test_preflight_for_the_active_version(switch_backend: tuple) -> None:
    _ob, cl, _env = switch_backend
    installed = _installed_appbuilder()
    r = cl.get(PRE, params={"version": "2.48.40.260702"})
    assert r.status_code == 200
    j = r.json()
    assert set(j) == {"target_version", "active_version", "required_appbuilder_version", "installed_appbuilder_version",
                      "candidate_versions", "install_required", "sticky_worker_running"}
    assert (j["target_version"], j["active_version"]) == ("2.48.40.260702", "2.48.40.260702")
    assert j["required_appbuilder_version"] == "2.48.40"
    assert j["candidate_versions"] == ["2.48.40", "2.49.0", "2.50.0", "2.51.0"], "A.B.C, then A.(B+1).0 .. A.(B+3).0"
    assert j["installed_appbuilder_version"] == (installed or None)
    assert j["install_required"] is (not installed or tuple(map(int, installed.split("."))) < (2, 48, 40))
    assert isinstance(j["sticky_worker_running"], bool)


@pytest.mark.parametrize(
    ("target", "required", "candidates"),
    [
        ("2.49.40.260810", "2.49.40", ["2.49.40", "2.50.0", "2.51.0", "2.52.0"]),
        ("2.51.40", "2.51.40", ["2.51.40", "2.52.0", "2.53.0", "2.54.0"]),
        ("2.10.1", "2.10.1", ["2.10.1", "2.11.0", "2.12.0", "2.13.0"]),
        ("2.3.7", "2.3.7", ["2.3.7", "2.4.0", "2.5.0", "2.6.0"]),
    ],
)
def test_preflight_derives_the_required_extension_and_four_candidates(switch_backend: tuple, target: str, required: str, candidates: list[str]) -> None:
    _ob, cl, _env = switch_backend
    j = cl.get(PRE, params={"version": target}).json()
    assert j["target_version"] == target and j["required_appbuilder_version"] == required
    assert j["candidate_versions"] == candidates
    assert j["active_version"] == "2.48.40.260702"


def test_preflight_install_required_is_decided_by_a_numeric_comparison(switch_backend: tuple) -> None:
    _ob, cl, _env = switch_backend
    installed = _installed_appbuilder()
    assert installed, "the backend interpreter has no qai_appbuilder; the install_required assertions need it"
    have = tuple(int(x) for x in installed.split("."))
    low = cl.get(PRE, params={"version": "2.3.7"}).json()       # needs 2.3.7  <= installed
    high = cl.get(PRE, params={"version": "2.51.40"}).json()    # needs 2.51.40 >  installed (2.48.40 here)
    assert low["install_required"] is (have < (2, 3, 7))
    assert high["install_required"] is (have < (2, 51, 40))


def test_preflight_rejects_a_version_that_is_not_installed(switch_backend: tuple) -> None:
    _ob, cl, _env = switch_backend
    r = cl.get(PRE, params={"version": "2.99.0"})
    assert r.status_code == 400 and r.json()["code"] == "qairt_switch.target_not_installed"
    assert "version" in _field_errors(r)


@pytest.mark.parametrize("bad", ["abc", "2.48", "v2.48.40", "2.48.40-beta", " 2.48.40", "2.48.40 ", "2.48.40.1.2", "-2.48.40", "2.48.+40", ""])
def test_preflight_rejects_malformed_versions(switch_backend: tuple, bad: str) -> None:
    _ob, cl, _env = switch_backend
    r = cl.get(PRE, params={"version": bad})
    assert r.status_code == 400 and r.json()["code"] in ("qairt_switch.invalid_target", "request.validation"), r.text
    assert "version" in " ".join(_field_errors(r))


def test_preflight_needs_the_version_query(switch_backend: tuple) -> None:
    _ob, cl, _env = switch_backend
    assert "query.version" in _field_errors(cl.get(PRE))


def test_preflight_changes_nothing(switch_backend: tuple) -> None:
    _ob, cl, env = switch_backend
    fake_sdk = Path(json.loads(env.read_text(encoding="utf-8"))["qairt_sdk_root"]).parent
    env_before, sdk_before = _sha(env), sorted(p.name for p in fake_sdk.iterdir())
    for v in ("2.48.40.260702", "2.49.40.260810", "2.99.0", "abc"):
        cl.get(PRE, params={"version": v})
    assert _sha(env) == env_before and sorted(p.name for p in fake_sdk.iterdir()) == sdk_before
    assert not list((env.parent).glob("qairt_env.json.*")) and not list(env.parent.glob("*.bak"))


# --------------------------------------------------------------------------
# create (validation edges only) and job lookup
# --------------------------------------------------------------------------


def test_create_requires_the_acknowledgement_before_anything_else(switch_backend: tuple) -> None:
    """The acknowledgement is checked FIRST: even an invalid target is reported as 'acknowledge first'."""
    _ob, cl, env = switch_backend
    before = _sha(env)
    for body in ({"target_version": "2.49.40.260810"}, {"target_version": "abc"}, {"target_version": "2.49.40.260810", "no_qnn_inference_acknowledged": False}):
        r = cl.post(SW, json=body)
        assert r.status_code == 400 and r.json()["code"] == "qairt_switch.acknowledgement_required"
        assert "no_qnn_inference_acknowledged" in _field_errors(r)
        assert "explicitly true" in r.json()["message"] and "QNN model inference" in r.json()["message"]
    assert _sha(env) == before


@pytest.mark.parametrize("target", ["x", "abc", "2.99.0", "2.48", "2.48.40-beta"])
def test_create_with_an_unknown_target_is_rejected_before_a_job_exists(switch_backend: tuple, target: str) -> None:
    _ob, cl, env = switch_backend
    before = _sha(env)
    r = cl.post(SW, json={"target_version": target, "no_qnn_inference_acknowledged": True})
    assert r.status_code == 400 and r.json()["code"] == "qairt_switch.invalid_target"
    assert "target_version" in _field_errors(r)
    assert _sha(env) == before, "no job ran: the env file is byte-identical"
    assert cl.get(PRE, params={"version": "2.48.40.260702"}).status_code == 200, "and the single switch slot is still free"


def test_create_body_validation(switch_backend: tuple) -> None:
    _ob, cl, _env = switch_backend
    assert "body.target_version" in _field_errors(cl.post(SW, json={}))
    assert "body.no_qnn_inference_acknowledged" in _field_errors(cl.post(SW, json={"target_version": "2.49.40.260810", "no_qnn_inference_acknowledged": "maybe"}))
    assert "body.target_version" in _field_errors(cl.post(SW, json={"target_version": 5}))


def test_job_lookup_for_an_unknown_id_is_404(switch_backend: tuple) -> None:
    _ob, cl, _env = switch_backend
    for jid in ("nope", "0" * 26, "x" * 200):
        r = cl.get(f"{SW}/{jid}")
        assert r.status_code == 404 and r.json()["code"] == "qairt_switch.job_not_found"
        assert r.json()["details"] == {"resource_type": "qairt_switch_job", "resource_id": jid}


def test_the_switch_routes_do_not_accept_other_methods(switch_backend: tuple) -> None:
    _ob, cl, _env = switch_backend
    assert cl.put(SW, json={}).status_code == 405 and cl.delete(f"{SW}/nope").status_code == 405
    assert cl.post(PRE, json={}).status_code == 405
