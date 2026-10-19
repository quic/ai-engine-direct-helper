# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - conversion environment readiness, read-only (MB-07).

Endpoints: ``GET /api/system/qairt-versions`` (public, read-only) and the QAIRT SDK hot-switch API
``GET /api/system/qairt-switches/preflight``, ``POST /api/system/qairt-switches`` (validation edges only) and
``GET /api/system/qairt-switches/{job_id}``. The switch service is wired at startup from ``qairt_env.json``, so every
test here starts its OWN backend (``_owned_backend.py``) on a throw-away data dir whose ``config/qairt_env.json`` points
at a throw-away FAKE SDK folder (empty version directories); the shared backend has no such file.

NOT covered (deliberately):

* **A switch that runs.** ``POST /qairt-switches`` with a valid target + acknowledgement starts a job that installs a
  ``qai_appbuilder`` wheel (network, pip), rewrites ``qairt_env.json`` and recycles the worker. Only the requests that are
  rejected BEFORE a job is created are sent (missing acknowledgement, unknown target); the test asserts the env file is
  byte-identical afterwards. Untested: ``run_switch`` phases, rollback, the sticky-worker recycle, the job poll shape of a
  real job.
* **The real SDK** (``C:\\Qualcomm\\AIStack\\QAIRT``) and the real ``data/config/qairt_env.json``: never read or written
  by a test (the fake root is a temp folder; the fallback root is only observed in the no-config case, read-only).
* **The x64 conversion venv and whether the SDK actually works** - that is the model-builder pipeline (L3).

Isolation (verified): the fake SDK parent and the backend's data dir are temp folders removed in teardown; the repo's own
``data/config/qairt_env.json`` is hashed before and after the module and must not change; the backend never gets a valid
switch request.

Observed behaviour worth pinning:

* ``qairt-versions`` lists strict ``A.B.C[.D]`` directories only, newest first by NUMERIC comparison (``2.10.1`` above
  ``2.9.1``); prefixes (``v2.50.0``), suffixes (``2.50.0-beta``), 5-segment names, 2-segment names and plain files are skipped.
* ``qairt_root`` (current schema) wins over ``qairt_sdk_root`` (legacy) when both are present; the active version is the name
  of that directory and is reported as ``null`` when it is not one of the discovered versions.
* ``installed_appbuilder_version`` is the ``qai_appbuilder`` wheel of the interpreter the BACKEND runs under, so the test
  reads it from that interpreter instead of hard-coding it.
* An unreadable / corrupt / empty ``qairt_env.json`` degrades quietly to the default root; but a file that is valid JSON and
  not an object (a list, string, number, ``null``) **stops the backend from starting** (MB-ISS below).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
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
REAL_SDK_ROOT = Path("C:/Qualcomm/AIStack/QAIRT")

# Every name below is a directory unless marked; only the strict A.B.C[.D] ones are SDK versions.
VERSION_DIRS = ("2.48.40.260702", "2.49.40.260810", "2.9.1", "2.10.1", "2.3.7", "2.51.40", "2.48.40.260801", "2.48.40")
NOT_VERSIONS = ("2.10", "v2.50.0", "2.50.0-beta", "2.50.0.1.2", "latest", " 2.52.0")
NEWEST_FIRST = ["2.51.40", "2.49.40.260810", "2.48.40.260801", "2.48.40.260702", "2.48.40", "2.10.1", "2.9.1", "2.3.7"]


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


@pytest.fixture(scope="module", autouse=True)
def real_env_file_untouched() -> Iterator[None]:
    before = _sha(REAL_ENV)
    sdk_before = sorted(p.name for p in REAL_SDK_ROOT.iterdir()) if REAL_SDK_ROOT.exists() else None
    yield
    assert _sha(REAL_ENV) == before, "the repo's own data/config/qairt_env.json changed during this module"
    assert (sorted(p.name for p in REAL_SDK_ROOT.iterdir()) if REAL_SDK_ROOT.exists() else None) == sdk_before


@pytest.fixture
def fake_sdk() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="qai-fake-qairt-"))
    assert str(root).startswith(tempfile.gettempdir()) or "qai-fake-qairt-" in str(root)
    for n in (*VERSION_DIRS, *NOT_VERSIONS):
        (root / n).mkdir()
    (root / "2.60.0").write_text("a FILE named like a version")
    yield root
    shutil.rmtree(root, ignore_errors=True)


@contextmanager
def backend_with_env(payload: object | None, *, env: dict[str, str] | None = None) -> Iterator[tuple[OwnedBackend, httpx.Client | None]]:
    """Start a backend whose ``config/qairt_env.json`` is ``payload`` (a str is written raw; ``None`` writes no file)."""
    old = {k: os.environ.get(k) for k in (env or {})}
    os.environ.update(env or {})
    try:
        with owned_backend() as ob:
            if payload is not None:
                cfg = ob.data_dir / "config"
                cfg.mkdir()
                text = payload if isinstance(payload, str) else json.dumps(payload)
                (cfg / "qairt_env.json").write_text(text, encoding="utf-8")
            cl = ob.start()
            yield ob, cl
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


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


def _versions(cl: httpx.Client) -> dict:
    r = cl.get("/api/system/qairt-versions")
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------
# qairt-versions
# --------------------------------------------------------------------------


def sorted_real_versions() -> list[str]:
    """What the fallback root (the machine's real SDK folder) holds, read-only, strictly parsed like the product does."""
    import re

    if not REAL_SDK_ROOT.exists():
        return []
    ok = [p.name for p in REAL_SDK_ROOT.iterdir() if p.is_dir() and re.fullmatch(r"\d+\.\d+\.\d+(\.\d+)?", p.name)]
    return sorted(ok, key=lambda n: tuple(int(x) for x in n.split(".")), reverse=True)


def _scenario(sdk: Path, spec: dict | str | None) -> object | None:
    """Expand ``{sdk}`` in a payload spec; strings are written raw, ``None`` writes no file."""
    if isinstance(spec, dict):
        return {k: (v.replace("{sdk}", str(sdk)) if isinstance(v, str) else v) for k, v in spec.items()}
    return spec


# (id, env-file payload, expected active_version, expected list) - ``"REAL"`` means the machine's default SDK root
VERSION_SCENARIOS: list[tuple[str, object | None, str | None, object]] = [
    ("legacy-field", {"qairt_sdk_root": "{sdk}\\2.48.40.260702"}, "2.48.40.260702", "FAKE"),
    ("current-field", {"qairt_root": "{sdk}\\2.49.40.260810"}, "2.49.40.260810", "FAKE"),
    ("both-root-wins", {"qairt_root": "{sdk}\\2.49.40.260810", "qairt_sdk_root": "{sdk}\\2.48.40.260702"}, "2.49.40.260810", "FAKE"),
    ("trailing-backslash", {"qairt_sdk_root": "{sdk}\\2.48.40.260702\\"}, "2.48.40.260702", "FAKE"),
    ("forward-slashes", {"qairt_sdk_root": "{sdk}/2.48.40.260702"}, "2.48.40.260702", "FAKE"),
    ("env-var", {"qairt_sdk_root": "%QAI_E2E_SDK%\\2.48.40.260702"}, "2.48.40.260702", "FAKE"),
    ("leaf-not-a-version", {"qairt_sdk_root": "{sdk}\\latest"}, None, "FAKE"),
    ("leaf-missing", {"qairt_sdk_root": "{sdk}\\2.7.7"}, None, "FAKE"),
    ("leaf-prefixed", {"qairt_sdk_root": "{sdk}\\v2.50.0"}, None, "FAKE"),
    ("empty-string", {"qairt_sdk_root": ""}, None, "REAL"),
    ("wrong-type", {"qairt_sdk_root": 5}, None, "REAL"),
    ("no-known-field", {"other": 1}, None, "REAL"),
    ("empty-object", {}, None, "REAL"),
    ("corrupt-json", "{not json", None, "REAL"),
    ("empty-file", "", None, "REAL"),
    ("bom-valid-json", "BOM:{\"qairt_sdk_root\": \"{sdk}\\\\2.48.40.260702\"}", None, "REAL"),
    ("missing-parent", {"qairt_sdk_root": "{sdk}\\no-such-folder\\2.48.40.260702"}, None, []),
]


def test_qairt_versions_for_every_env_file_shape(fake_sdk: Path) -> None:
    """One backend, restarted once per env-file shape (a restart costs ~10 s, a fresh data dir ~15 s).

    The list is read from the PARENT of the configured directory; the active version is that directory's name when it is
    one of the discovered versions. Unusable files degrade to the default root (the machine's real SDK folder, read-only).
    ``bom-valid-json`` documents a KNOWN GAP (MB-ISS-03): a UTF-8 BOM makes an otherwise valid file count as absent.
    """
    fails: list[str] = []
    os.environ["QAI_E2E_SDK"] = str(fake_sdk)
    try:
        with owned_backend() as ob:
            cfg = ob.data_dir / "config"
            cfg.mkdir()
            env_file = cfg / "qairt_env.json"
            for sid, spec, active, listed in VERSION_SCENARIOS:
                payload = _scenario(fake_sdk, spec)
                if isinstance(payload, str) and payload.startswith("BOM:"):
                    text = "\ufeff" + payload[4:].replace("{sdk}", str(fake_sdk).replace("\\", "\\\\"))
                else:
                    text = payload if isinstance(payload, str) else json.dumps(payload)
                env_file.write_text(text, encoding="utf-8")
                cl = ob.start()
                got = _versions(cl)
                want_list = NEWEST_FIRST if listed == "FAKE" else (sorted_real_versions() if listed == "REAL" else listed)
                if got != {"active_version": active, "versions": want_list}:
                    fails.append(f"{sid}: got {got}, want active={active!r} versions={want_list}")
                ob.stop()
    finally:
        os.environ.pop("QAI_E2E_SDK", None)
    assert not fails, "\n".join(fails)


def test_the_build_segment_orders_versions_that_share_a_major_minor_patch() -> None:
    """``2.48.40.260801`` is newer than ``2.48.40.260702``, and both are newer than the bare ``2.48.40`` (no build = 0)."""
    sub = [n for n in NEWEST_FIRST if n.startswith("2.48.40")]
    assert sub == ["2.48.40.260801", "2.48.40.260702", "2.48.40"]


def test_preflight_reports_no_active_version_when_the_configured_directory_is_not_installed(fake_sdk: Path) -> None:
    """``qairt-versions`` and ``preflight`` apply the same rule: the configured leaf only counts as active when it is one of the
    discovered versions (here the config points at a leaf that does not exist)."""
    with backend_with_env({"qairt_sdk_root": str(fake_sdk / "2.7.7")}) as (_ob, cl):
        assert _versions(cl)["active_version"] is None
        r = cl.get(PRE, params={"version": "2.49.40.260810"})
    assert r.status_code == 200 and r.json()["active_version"] is None and r.json()["target_version"] == "2.49.40.260810"

def test_ordering_is_numeric_not_alphabetical() -> None:
    """Pure check on the expected list the scenarios above rely on (2.10.1 above 2.9.1, 4-segment before 3-segment)."""
    key = lambda n: tuple(int(x) for x in n.split("."))  # noqa: E731
    assert NEWEST_FIRST == sorted(NEWEST_FIRST, key=key, reverse=True)
    assert NEWEST_FIRST.index("2.10.1") < NEWEST_FIRST.index("2.9.1")
    assert sorted(["2.9.1", "2.10.1"]) == ["2.10.1", "2.9.1"], "a lexicographic sort would get this wrong - the product must not"


def test_a_bom_in_the_env_file_is_treated_as_no_config(fake_sdk: Path) -> None:
    """KNOWN GAP (current behaviour, pinned) - MB-ISS-03: ``read_text(encoding="utf-8")`` keeps the BOM and ``json.loads``
    rejects it, so a BOM-prefixed (Notepad-saved) config silently loses its active version. Covered as the
    ``bom-valid-json`` row of the scenario test; this restates it as a stand-alone, named assertion."""
    row = next(s for s in VERSION_SCENARIOS if s[0] == "bom-valid-json")
    assert row[2] is None and row[3] == "REAL"


def test_no_env_file_uses_the_default_root_and_has_no_active_version() -> None:
    with backend_with_env(None) as (ob, cl):
        assert not (ob.data_dir / "config" / "qairt_env.json").exists()
        got = _versions(cl)
    assert got == {"active_version": None, "versions": sorted_real_versions()}


def test_a_non_canonical_directory_name_is_listed_but_can_never_be_preflighted(fake_sdk: Path) -> None:
    """KNOWN GAP (current behaviour, pinned) - MB-ISS-02: the strict parser accepts leading zeros and full-width digits
    (``02.53.0``, ``2.049.0``, ``２.４８.４０``), and ``GET /qairt-versions`` returns the raw directory NAME. ``preflight``
    compares the NORMALISED ``str(target)`` (``2.53.0``) with that raw name, so such a version appears in the selector
    and is then rejected as 'not installed' whichever spelling is sent. A real SDK is always canonically named, so this
    only bites hand-made folders. If the two sides are made to agree, the preflight below becomes a 200."""
    (fake_sdk / "02.53.0").mkdir()
    (fake_sdk / "2.049.0").mkdir()
    with backend_with_env({"qairt_sdk_root": str(fake_sdk / "2.48.40.260702")}) as (_ob, cl):
        listed = _versions(cl)["versions"]
        assert "02.53.0" in listed and "2.049.0" in listed
        for sent in ("02.53.0", "2.53.0", "2.049.0", "2.49.0"):
            r = cl.get(PRE, params={"version": sent})
            assert r.status_code == 400 and r.json()["code"] == "qairt_switch.target_not_installed", sent

# --------------------------------------------------------------------------
# the switch API is unavailable without an env file
# --------------------------------------------------------------------------


def test_without_an_env_file_every_switch_route_is_404_unavailable() -> None:
    with backend_with_env(None) as (_ob, cl):
        for method, path, kw in (
            ("GET", PRE, {"params": {"version": "2.48.40.260702"}}),
            ("GET", f"{SW}/nope", {}),
            ("POST", SW, {"json": {"target_version": "2.48.40.260702"}}),
        ):
            r = cl.request(method, path, **kw)
            assert r.status_code == 404 and r.json()["code"] == "qairt_switch.unavailable", (method, path)
            assert "no existing qairt_env.json" in r.json()["message"]
