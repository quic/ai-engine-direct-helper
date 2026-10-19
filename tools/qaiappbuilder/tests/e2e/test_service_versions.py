# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - Service version management ``/api/versions/*`` (SVC-04), everything that needs no real network.

Endpoints (``interfaces/http/routes/versions.py``): ``GET /api/versions`` (release catalog),
``GET|PUT /download-settings``, ``POST /service-install``, ``DELETE /install/{v}``,
``GET /install/{v}/running``, ``DELETE /download/{v}``, ``GET /local-status``, plus the validation
edges of ``POST /download``.

How the network is avoided: the catalog URL is a setting, so tests point it at a throw-away HTTP
server owned by the test on ``127.0.0.1``. Installs use zip files the test builds in a temp dir.

NOT covered (deliberately):

* **A real download** (``POST /download`` with a valid body). The engine prefers aria2c, which
  auto-DOWNLOADS ``aria2c.exe`` from the internet and starts an RPC daemon on port 6800, and the
  httpx fallback needs a reachable file. Only the 400 validation edges run - they are raised before
  the engine is touched. Untested: progress frames, cancel, checksum mismatch, resume, the aria2c
  path and the stall watchdog.
* **The "service is running" detection** and the kill-and-retry delete fallback. Both enumerate
  processes with ``wmic``, which is not present on current Windows 11 builds (it is absent on this
  machine), so ``/install/{v}/running`` is always ``false`` here and the kill path can never fire.
  A positive case would also need a live process under the install dir.
* **A stale/locked directory** (WinError 5 handling), the ``.bak`` rollback on a LOCKED rename, and
  the multi-GB / long-path (>260 chars) extraction cases.
* **The official release manifest itself** (github.com).

Isolation (verified): every file lives in the throw-away data dir (``bin/``, ``downloads/``,
``config/forge_config.json``); an autouse fixture empties them before and after each test and
refuses to run unless the data dir is a ``qai-e2e-*`` temp dir. Nothing is started (no
GenieAPIService, no aria2c); the module asserts that no ``aria2c`` process or port 6800 listener
appeared.

Observed behaviour worth pinning (it differs from what one might assume):

* **``DELETE /download/%2e%2e`` deletes ``*.zip`` files in the PARENT of the downloads dir** (path
  traversal; the version segment is joined onto the downloads dir unchecked). A backslash form
  also works on Windows (``a``, backslash, ``..``, backslash, ``keep``); an encoded forward slash
  does not (route mismatch).
* ``DELETE /install/{v}`` matches ``v`` as a **substring of the directory name**, not as a parsed
  version: ``2.44.0`` (the QAIRT version inside ``..._v2.3.7_QAIRT_v2.44.0_v73``) deletes the
  2.3.7 install, ``Genie`` or ``e`` deletes every install. Only a digit/dot boundary is enforced.
* ``GET /local-status`` has a **write side effect**: when ``genie_service.root_path`` is unset it
  writes the newest install into ``forge_config.json`` - which then decides which
  ``service_config.json`` the Service page edits (see ``test_service.py``). The pointer is never
  healed: deleting that install leaves it dangling.
* ``PUT /download-settings`` is a **full replace**, not a patch: omitted fields go back to defaults.
  Empty URLs read back as the github defaults; ``fetch_timeout_seconds: 0`` reads back as 15;
  negative timeouts and non-http URLs are stored.
* The release manifest's ``tags`` given as a STRING is exploded into single characters.
* Install **deletes the source zip** the caller passed (and its parent dir if that becomes empty).
"""

from __future__ import annotations

import http.server
import json
import shutil
import socket
import threading
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import Backend

pytestmark = pytest.mark.e2e

V = "/api/versions"


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------


@pytest.fixture
def api(http: httpx.Client) -> httpx.Client:
    http.get("/api/system/uptime")
    http.headers["X-QAI-CSRF"] = http.cookies.get("qai_csrf")
    return http


def _aria2c_running() -> bool:
    import subprocess

    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq aria2c.exe"], capture_output=True, text=True).stdout
    if "aria2c.exe" in out.lower():
        return True
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", 6800)) == 0


@pytest.fixture(scope="module", autouse=True)
def no_download_machinery_started() -> Iterator[None]:
    """Nothing in this module may start aria2c (it would also download its binary)."""
    was_running = _aria2c_running()
    yield
    if not was_running:
        assert not _aria2c_running(), "aria2c was started during this module (a real download was triggered)"


@pytest.fixture(autouse=True)
def clean_state(backend: Backend) -> Iterator[None]:
    root = backend.data_dir
    assert "qai-e2e-" in str(root), f"refusing to touch a non-throw-away data dir: {root}"

    def _wipe() -> None:
        for sub in ("bin", "downloads"):
            shutil.rmtree(root / sub, ignore_errors=True)
        (root / "config" / "forge_config.json").unlink(missing_ok=True)
        (root / "stray.zip").unlink(missing_ok=True)

    _wipe()
    yield
    _wipe()


def _bin(backend: Backend) -> list[str]:
    d = backend.data_dir / "bin"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def _forge_file(backend: Backend) -> Path:
    return backend.data_dir / "config" / "forge_config.json"


def _forge(backend: Backend) -> dict:
    f = _forge_file(backend)
    return json.loads(f.read_text(encoding="utf-8-sig")) if f.exists() else {}


def _mkzip(path: Path, files: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


def _mkinstall(backend: Backend, name: str, *, exe: bool = True) -> Path:
    d = backend.data_dir / "bin" / name
    d.mkdir(parents=True, exist_ok=True)
    if exe:
        (d / "GenieAPIService.exe").write_text("placeholder - never executed")
    return d


def _local(api: httpx.Client) -> dict:
    r = api.get(f"{V}/local-status")
    assert r.status_code == 200, r.text
    return r.json()


def _field_errors(r: httpx.Response) -> dict:
    assert r.status_code == 400, r.text
    return r.json()["details"]["field_errors"]


class _Catalog:
    """A throw-away HTTP server standing in for the release-manifest host."""

    def __init__(self) -> None:
        outer = self
        self.code = 200
        self.body = ""
        self.delay = 0.0
        self.hits = 0

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                outer.hits += 1
                time.sleep(outer.delay)
                data = outer.body.encode()
                try:
                    self.send_response(outer.code)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

            def log_message(self, *a: object) -> None:
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/manifest"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def catalog() -> Iterator[_Catalog]:
    c = _Catalog()
    yield c
    c.close()


def _settings(api: httpx.Client, **over: object) -> dict:
    body = {"version_list_url": "", "catalog_url": "", "fetch_timeout_seconds": 5, "download_timeout_seconds": 20, **over}
    r = api.put(f"{V}/download-settings", json=body)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def served(api: httpx.Client, catalog: _Catalog) -> _Catalog:
    _settings(api, version_list_url=catalog.url, catalog_url=catalog.url)
    return catalog


GITHUB_VERSIONS = "https://github.com/qualcomm/qai-appbuilder/releases/download/v2.34.0/release_manifest.json"
GITHUB_CATALOG = "https://github.com/qualcomm/qai-appbuilder/releases/download/v2.34.0/model_catalog.json"


# --------------------------------------------------------------------------
# download settings
# --------------------------------------------------------------------------


def test_settings_defaults(api: httpx.Client) -> None:
    assert api.get(f"{V}/download-settings").json() == {
        "save_dir": "", "version_list_url": GITHUB_VERSIONS, "catalog_url": GITHUB_CATALOG,
        "fetch_timeout_seconds": 15, "download_timeout_seconds": 300, "ssl_verify": False,
    }


def test_settings_round_trip_and_persistence(api: httpx.Client, backend: Backend) -> None:
    body = {"save_dir": "D:/dl", "version_list_url": "http://a.invalid/v", "catalog_url": "http://a.invalid/c",
            "fetch_timeout_seconds": 7, "download_timeout_seconds": 99, "ssl_verify": True}
    r = api.put(f"{V}/download-settings", json=body)
    assert r.status_code == 200 and r.json() == body
    assert api.get(f"{V}/download-settings").json() == body
    on_disk = _forge(backend)
    assert on_disk["download"]["save_dir"] == "D:/dl"
    assert on_disk["version_check"] == {"version_list_url": "http://a.invalid/v", "fetch_timeout_seconds": 7,
                                        "download_timeout_seconds": 99, "ssl_verify": True}
    assert on_disk["model_catalog"] == {"catalog_url": "http://a.invalid/c"}


def test_settings_put_is_a_full_replace_not_a_patch(api: httpx.Client) -> None:
    _settings(api, version_list_url="http://a.invalid/v", fetch_timeout_seconds=7, ssl_verify=True)
    r = api.put(f"{V}/download-settings", json={})
    assert r.status_code == 200
    assert api.get(f"{V}/download-settings").json() == {
        "save_dir": "", "version_list_url": GITHUB_VERSIONS, "catalog_url": GITHUB_CATALOG,
        "fetch_timeout_seconds": 15, "download_timeout_seconds": 300, "ssl_verify": False,
    }


def test_settings_keep_unrelated_sections_in_the_file(api: httpx.Client, backend: Backend) -> None:
    f = _forge_file(backend)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"security": {"bind_host": "127.0.0.1"}, "genie_service": {"root_path": "keep-me"},
                             "version_check": {"other_key": 1}}), encoding="utf-8")
    _settings(api, version_list_url="http://a.invalid/v")
    on_disk = _forge(backend)
    assert on_disk["security"] == {"bind_host": "127.0.0.1"}
    assert on_disk["genie_service"] == {"root_path": "keep-me"}
    assert on_disk["version_check"]["other_key"] == 1, "unknown keys inside a section survive"
    assert on_disk["version_check"]["version_list_url"] == "http://a.invalid/v"


def test_settings_file_format(api: httpx.Client, backend: Backend) -> None:
    _settings(api, save_dir="D:/下载")
    raw = _forge_file(backend).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")
    assert "D:/下载" in text, "non-ASCII is written literally"
    assert text.splitlines()[1].startswith('  "') and not text.splitlines()[1].startswith("   "), "indent is 2 spaces"


@pytest.mark.parametrize("content", [b"\xef\xbb\xbf" + json.dumps({"download": {"save_dir": "bom-dir"}}).encode()])
def test_settings_read_a_bom_file(api: httpx.Client, backend: Backend, content: bytes) -> None:
    f = _forge_file(backend)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(content)
    assert api.get(f"{V}/download-settings").json()["save_dir"] == "bom-dir"


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", "", '"a string"'])
def test_settings_unreadable_file_falls_back_to_defaults(api: httpx.Client, backend: Backend, content: str) -> None:
    f = _forge_file(backend)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    got = api.get(f"{V}/download-settings")
    assert got.status_code == 200 and got.json()["version_list_url"] == GITHUB_VERSIONS


def test_settings_odd_values_are_stored_without_validation(api: httpx.Client) -> None:
    """KNOWN GAP (current behaviour, pinned): no range or scheme validation on save."""
    _settings(api, version_list_url="ftp://x", catalog_url="not a url", fetch_timeout_seconds=0, download_timeout_seconds=-5)
    got = api.get(f"{V}/download-settings").json()
    assert got["version_list_url"] == "ftp://x" and got["catalog_url"] == "not a url"
    assert got["fetch_timeout_seconds"] == 15, "0 is treated as 'unset' and reads back as the default"
    assert got["download_timeout_seconds"] == -5, "a negative timeout is stored as-is"


@pytest.mark.parametrize("body", [{"fetch_timeout_seconds": "abc"}, {"download_timeout_seconds": 1.5}, {"ssl_verify": "maybe"}, {"save_dir": 5}])
def test_settings_reject_wrong_types(api: httpx.Client, body: dict) -> None:
    errs = _field_errors(api.put(f"{V}/download-settings", json=body))
    assert any(k.startswith("body.") for k in errs)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("yes", True), ("1", True), (" True ", True), ("TRUE", True), ("false", False), ("0", False), ("nope", False),
     (True, True), (False, False), (1, False)],
)
def test_settings_ssl_verify_is_coerced_from_the_file(api: httpx.Client, backend: Backend, raw: object, expected: bool) -> None:
    """A hand-edited forge_config.json may hold strings; only true/1/yes (any case, trimmed) mean True,
    and an int is NOT a bool (falls back to the default False)."""
    f = _forge_file(backend)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"version_check": {"ssl_verify": raw}}), encoding="utf-8")
    assert api.get(f"{V}/download-settings").json()["ssl_verify"] is expected


def test_settings_live_in_a_file_the_forge_config_api_does_not_show(api: httpx.Client) -> None:
    """KNOWN GAP (current behaviour, pinned): the download section is stored in forge_config.json, but
    ``GET /api/forge-config`` reads a different store, so it never shows version_check/download."""
    _settings(api, version_list_url="http://a.invalid/v", save_dir="D:/dl")
    cfg = api.get("/api/forge-config").json()["config"]
    assert "version_check" not in cfg and "download" not in cfg


# --------------------------------------------------------------------------
# release catalog: GET /api/versions
# --------------------------------------------------------------------------

MANIFEST = {"releases": [
    {"version": "2.48.40", "download_url": "https://example.invalid/a.zip", "release_date": "2026-01-01",
     "checksum_sha256": "ab", "size_bytes": 123, "is_recommended": True, "min_driver_version": "1.0",
     "changelog": {"en": "x", "zh-CN": "y"}, "tags": ["t", 5], "description": "d"},
    {"version": "2.48.40", "download_url": "https://example.invalid/dup.zip"},
    {"version": "bad", "download_url": "https://example.invalid/b.zip"},
    {"version": "2.0abc", "download_url": "https://example.invalid/b2.zip"},
    {"version": "1.0-beta", "download_url": "https://example.invalid/b3.zip"},
    {"version": "2.3", "download_url": "ftp://example.invalid/c.zip"},
    {"version": "3.0", "download_url": "", "packages": [
        {"platform": "v81", "platform_id": "v81", "download_url": "https://example.invalid/p.zip", "is_default": True},
        {"download_url": "ftp://no"}, "notadict",
        {"platform": "v82", "platform_id": "v82", "download_url": "HTTPS://EXAMPLE.INVALID/q.zip"}]},
    "notadict",
    {"version": "4", "download_url": "HTTP://EXAMPLE.invalid/UPPER.zip", "size_bytes": "12"},
]}


def _versions(api: httpx.Client) -> list[dict]:
    r = api.get(V)
    assert r.status_code == 200, r.text
    return r.json()["versions"]


def test_catalog_json_manifest_is_filtered_and_normalised(api: httpx.Client, served: _Catalog) -> None:
    served.body = json.dumps(MANIFEST)
    got = _versions(api)
    assert [v["version"] for v in got] == ["2.48.40", "3.0", "4"], "manifest order is kept; dup/invalid/non-http dropped"
    first = got[0]
    assert first["download_url"] == "https://example.invalid/a.zip" and first["size_bytes"] == 123
    assert first["is_recommended"] is True and first["changelog"] == {"en": "x", "zh-CN": "y"}
    assert first["tags"] == ["t"], "non-string tags are dropped"
    assert set(first) >= {"version", "download_url", "release_date", "checksum_sha256", "size_bytes", "is_recommended",
                          "min_driver_version", "changelog", "tags", "description", "packages"}
    packs = got[1]["packages"]
    assert [p["platform_id"] for p in packs] == ["v81", "v82"] and packs[0]["is_default"] is True, "only valid packages survive"
    assert packs[1]["download_url"].startswith("HTTPS://"), "a package url scheme check is case-insensitive too"
    assert got[1]["download_url"] == "" and got[2]["size_bytes"] == 12, "size_bytes is coerced to int"
    assert got[2]["download_url"].startswith("HTTP://"), "the scheme check is case-insensitive"


def test_catalog_string_tags_are_exploded_into_characters(api: httpx.Client, served: _Catalog) -> None:
    """KNOWN GAP (current behaviour, pinned): ``tags: "beta"`` becomes ``["b","e","t","a"]``."""
    served.body = json.dumps({"releases": [{"version": "1.0", "download_url": "http://x/y.zip", "tags": "beta"}]})
    assert _versions(api)[0]["tags"] == ["b", "e", "t", "a"]


def test_catalog_plain_text_list(api: httpx.Client, served: _Catalog) -> None:
    served.body = ("2.1.0 https://example.invalid/a.zip\n2.1.0 https://example.invalid/dup.zip\nbad https://x\n"
                   "2.0 ftp://nope\nonlyone\n\n1.9 http://example.invalid/old.zip extra tokens\n"
                   "3.0 HTTP://EXAMPLE.invalid/UP.zip\n2.0abc https://example.invalid/suffix.zip")
    got = _versions(api)
    assert [(v["version"], v["download_url"]) for v in got] == [
        ("2.1.0", "https://example.invalid/a.zip"), ("1.9", "http://example.invalid/old.zip"),
        ("3.0", "HTTP://EXAMPLE.invalid/UP.zip")]
    assert got[0]["packages"] == [] and got[0]["size_bytes"] == 0


@pytest.mark.parametrize(
    ("code", "body", "message"),
    [
        (200, "   \n ", "empty"),
        (404, "nope", "HTTP 404"),
        (500, "boom", "HTTP 500"),
        (200, '{"releases": [oops', "Invalid JSON manifest"),
        (200, '{"releases": 5}', "'releases' must be a list"),
        (200, '{"nothing": 1}', "'releases' must be a list"),
        (200, '{"releases": [{"version": "x"}]}', "No valid versions"),
        (200, "[1,2]", "No valid versions"),
        (200, "hello world\nfoo bar", "No valid versions"),
    ],
)
def test_catalog_content_problems_are_422(api: httpx.Client, served: _Catalog, code: int, body: str, message: str) -> None:
    served.code, served.body = code, body
    r = api.get(V)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "service_release.catalog_unavailable"
    assert message.lower() in r.json()["message"].lower()


def test_catalog_connection_refused_is_503(api: httpx.Client) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    _settings(api, version_list_url=f"http://127.0.0.1:{port}/x")
    r = api.get(V)
    assert r.status_code == 503 and r.json()["code"] == "service_release.catalog_connect_error"


def test_catalog_timeout_is_503_and_honours_the_setting(api: httpx.Client, served: _Catalog) -> None:
    served.body, served.delay = "2.0 http://example.invalid/a.zip", 3.0
    _settings(api, version_list_url=served.url, fetch_timeout_seconds=1)
    t0 = time.monotonic()
    r = api.get(V)
    elapsed = time.monotonic() - t0
    assert r.status_code == 503 and r.json()["code"] == "service_release.catalog_timeout"
    assert r.json()["details"]["timeout_s"] == 1.0
    assert elapsed < 2.8, f"the 1s fetch timeout was not honoured ({elapsed:.1f}s)"


def test_catalog_unsupported_scheme_is_503(api: httpx.Client) -> None:
    _settings(api, version_list_url="ftp://x/y")
    r = api.get(V)
    assert r.status_code == 503 and r.json()["code"] == "service_release.catalog_request_error"


def test_catalog_url_change_takes_effect_immediately(api: httpx.Client, served: _Catalog) -> None:
    served.body = "1.0 http://example.invalid/a.zip"
    assert [v["version"] for v in _versions(api)] == ["1.0"]
    served.body = "2.0 http://example.invalid/b.zip"
    assert [v["version"] for v in _versions(api)] == ["2.0"], "nothing is cached between calls"
    assert served.hits == 2


# --------------------------------------------------------------------------
# POST /api/versions/download: validation edges only (a valid request would start aria2c)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"version": "", "download_url": "http://x/y.zip"}, "version"),
        ({"version": "   ", "download_url": "http://x/y.zip"}, "version"),
        ({"version": "1.0", "download_url": "ftp://x/y.zip"}, "download_url"),
        ({"version": "1.0", "download_url": ""}, "download_url"),
        ({"version": "1.0", "download_url": "file:///C:/x.zip"}, "download_url"),
    ],
)
def test_download_rejects_bad_requests_before_any_engine_starts(api: httpx.Client, body: dict, field: str) -> None:
    r = api.post(f"{V}/download", json=body)
    assert r.status_code == 400 and r.headers["content-type"].startswith("application/json")
    assert r.json()["code"] == "service_release.invalid_download_request"
    assert field in r.json()["details"]["field_errors"]


@pytest.mark.parametrize(("body", "field"), [({"version": "1.0"}, "body.download_url"), ({"download_url": "http://x"}, "body.version")])
def test_download_missing_fields(api: httpx.Client, body: dict, field: str) -> None:
    assert field in _field_errors(api.post(f"{V}/download", json=body))


# --------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------


@pytest.mark.parametrize("save_path", ["", "   "])
def test_install_requires_a_save_path(api: httpx.Client, save_path: str) -> None:
    r = api.post(f"{V}/service-install", json={"save_path": save_path})
    assert r.status_code == 400 and "save_path" in r.json()["details"]["field_errors"]


def test_install_missing_field(api: httpx.Client) -> None:
    assert "body.save_path" in _field_errors(api.post(f"{V}/service-install", json={}))


def test_install_rejects_missing_non_zip_and_corrupt_archives(api: httpx.Client, backend: Backend, tmp_path: Path) -> None:
    txt = tmp_path / "x.txt"
    txt.write_text("x")
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip at all")
    for path, needle in ((tmp_path / "nope.zip", "invalid save_path"), (txt, "invalid save_path"), (bad, "corrupt archive")):
        r = api.post(f"{V}/service-install", json={"save_path": str(path), "version": "1.0"})
        assert r.status_code == 422 and r.json()["code"] == "service_release.install_failed", r.text
        assert needle in r.json()["message"]
    assert _bin(backend) == [], "a failed install leaves no half-made directory"
    assert bad.exists() and txt.exists(), "a failed install must not delete the source file"


@pytest.mark.parametrize(
    ("stem", "version", "expected_dir"),
    [
        ("GenieAPIService_v2.3.7_QAIRT_v2.44.0_v73", "2.3.7", "GenieAPIService_v2.3.7_QAIRT_v2.44.0_v73"),
        ("GenieService-win-arm64", "2.48.40", "GenieAPIService_v2.48.40_GenieService-win-arm64"),
        ("GenieAPIService_v9.0_other", "2.0", "GenieAPIService_v2.0_GenieAPIService_v9.0_other"),
        ("my model", "5.5", "GenieAPIService_v5.5_my-model"),
        ("x", "", "x"),
        ("GenieAPIService_v1.2", "1.2; rm -rf", "GenieAPIService_v1.2"),
    ],
)
def test_install_directory_name_always_carries_the_version(
    api: httpx.Client, backend: Backend, tmp_path: Path, stem: str, version: str, expected_dir: str
) -> None:
    z = _mkzip(tmp_path / "dl" / f"{stem}.zip", {"GenieAPIService.exe": "exe", "Genie.dll": "d"})
    r = api.post(f"{V}/service-install", json={"save_path": str(z), "version": version})
    assert r.status_code == 200, r.text
    body = r.json()
    assert _bin(backend) == [expected_dir]
    assert body["root_path"] == str(backend.data_dir / "bin" / expected_dir)
    assert body["exe_path"] == str(backend.data_dir / "bin" / expected_dir / "GenieAPIService.exe")
    assert body["version"] == version and body["ok"] is True


def test_install_flattens_a_single_top_level_directory(api: httpx.Client, backend: Backend, tmp_path: Path) -> None:
    z = _mkzip(tmp_path / "dl" / "G.zip", {"Top/GenieAPIService.exe": "e", "Top/service_config.json": "{}", "Top/sub/a.txt": "a"})
    assert api.post(f"{V}/service-install", json={"save_path": str(z), "version": "1.0"}).status_code == 200
    inst = backend.data_dir / "bin" / _bin(backend)[0]
    assert sorted(p.name for p in inst.iterdir()) == ["GenieAPIService.exe", "service_config.json", "sub"]
    assert (inst / "sub" / "a.txt").read_text() == "a"


def test_install_keeps_several_top_level_entries_as_they_are(api: httpx.Client, backend: Backend, tmp_path: Path) -> None:
    z = _mkzip(tmp_path / "dl" / "x.zip", {"a/GenieAPIService.exe": "1", "b/other.txt": "2"})
    assert api.post(f"{V}/service-install", json={"save_path": str(z)}).status_code == 200
    inst = backend.data_dir / "bin" / "x"
    assert sorted(p.name for p in inst.iterdir()) == ["a", "b"]


def test_install_deletes_the_source_zip_and_its_empty_parent(api: httpx.Client, tmp_path: Path) -> None:
    z = _mkzip(tmp_path / "alone" / "G.zip", {"GenieAPIService.exe": "e"})
    r = api.post(f"{V}/service-install", json={"save_path": str(z), "version": "1.0"})
    assert r.json()["zip_deleted"] is True
    assert not z.exists() and not z.parent.exists(), "the empty download folder is removed too"


def test_install_keeps_a_parent_directory_that_still_has_files(api: httpx.Client, tmp_path: Path) -> None:
    z = _mkzip(tmp_path / "shared" / "G.zip", {"GenieAPIService.exe": "e"})
    (tmp_path / "shared" / "keep.txt").write_text("mine")
    assert api.post(f"{V}/service-install", json={"save_path": str(z), "version": "1.0"}).json()["zip_deleted"] is True
    assert not z.exists() and (tmp_path / "shared" / "keep.txt").read_text() == "mine"


def test_reinstall_replaces_the_previous_install_and_leaves_no_backup(api: httpx.Client, backend: Backend, tmp_path: Path) -> None:
    old = _mkzip(tmp_path / "d1" / "GenieService-win-arm64.zip", {"GenieAPIService.exe": "OLD", "old_only.txt": "o"})
    new = _mkzip(tmp_path / "d2" / "GenieService-win-arm64.zip", {"GenieAPIService.exe": "NEW", "new_only.txt": "n"})
    for z in (old, new):
        assert api.post(f"{V}/service-install", json={"save_path": str(z), "version": "2.48.40"}).status_code == 200
    assert len(_bin(backend)) == 1 and not _bin(backend)[0].endswith(".bak")
    inst = backend.data_dir / "bin" / _bin(backend)[0]
    assert (inst / "GenieAPIService.exe").read_text() == "NEW"
    assert not (inst / "old_only.txt").exists() and (inst / "new_only.txt").exists()


def test_failed_reinstall_restores_the_working_install(api: httpx.Client, backend: Backend, tmp_path: Path) -> None:
    good = _mkzip(tmp_path / "d1" / "G.zip", {"GenieAPIService.exe": "GOOD"})
    assert api.post(f"{V}/service-install", json={"save_path": str(good), "version": "3.0"}).status_code == 200
    bad = tmp_path / "d2" / "G.zip"
    bad.parent.mkdir()
    bad.write_bytes(b"corrupt")
    r = api.post(f"{V}/service-install", json={"save_path": str(bad), "version": "3.0"})
    assert r.status_code == 422 and "corrupt archive" in r.json()["message"]
    names = _bin(backend)
    assert len(names) == 1 and not names[0].endswith(".bak"), names
    assert (backend.data_dir / "bin" / names[0] / "GenieAPIService.exe").read_text() == "GOOD"


def test_install_cannot_write_outside_the_install_directory(api: httpx.Client, backend: Backend, tmp_path: Path) -> None:
    z = _mkzip(tmp_path / "dl" / "zip slip.zip", {"../../evil.txt": "pwn", "ok.txt": "ok"})
    assert api.post(f"{V}/service-install", json={"save_path": str(z), "version": "9.9"}).status_code == 200
    inst = backend.data_dir / "bin" / _bin(backend)[0]
    assert sorted(str(p) for p in backend.data_dir.rglob("evil.txt")) == [str(inst / "evil.txt")]
    assert not list(tmp_path.rglob("evil.txt"))


# --------------------------------------------------------------------------
# local-status
# --------------------------------------------------------------------------


def test_local_status_empty(api: httpx.Client) -> None:
    assert _local(api) == {"versions": {}, "auto_configured": False, "auto_configured_path": ""}


def test_local_status_downloaded_scan(api: httpx.Client, backend: Backend) -> None:
    dl = backend.data_dir / "downloads"
    _mkzip(dl / "7.0" / "Genie_v81.zip", {"a": "b"})
    _mkzip(dl / "7.1" / "Genie_v73.zip", {"a": "b"})
    (dl / "7.1" / "Genie_v73.zip.aria2").write_text("partial")
    _mkzip(dl / "notaversion" / "x.zip", {"a": "b"})
    _mkzip(dl / "7.2" / "a.zip", {"a": "b"})
    _mkzip(dl / "7.2" / "b.zip", {"a": "b"})
    (dl / "7.3").mkdir()
    (dl / "7.3" / "readme.txt").write_text("x")
    got = _local(api)["versions"]
    assert set(got) == {"7.0", "7.2"}, "partial (.aria2), non-version dirs and zip-less dirs are not 'downloaded'"
    assert got["7.0"] == {"downloaded": True, "save_path": str(dl / "7.0" / "Genie_v81.zip"), "installed": False,
                          "install_path": "", "platform_driver": "v81"}
    assert got["7.2"]["downloaded"] is True and got["7.2"]["platform_driver"] == ""


def test_local_status_installed_scan(api: httpx.Client, backend: Backend) -> None:
    _mkinstall(backend, "GenieAPIService_v2.3.7_QAIRT_v2.44.0_v73")
    _mkinstall(backend, "GenieAPIService_v2.48.40_GenieService-win-arm64")
    _mkinstall(backend, "GenieAPIService_v8.8.bak")
    _mkinstall(backend, "GenieAPIService_v6.6", exe=False)
    _mkinstall(backend, "no-version-in-name")
    got = _local(api)["versions"]
    assert set(got) == {"2.3.7", "2.48.40"}, "only dirs with the exe and a version in the name count; .bak is ignored"
    assert got["2.3.7"]["installed"] is True and got["2.3.7"]["platform_driver"] == "v73"
    assert got["2.3.7"]["install_path"] == str(backend.data_dir / "bin" / "GenieAPIService_v2.3.7_QAIRT_v2.44.0_v73")
    assert got["2.48.40"]["platform_driver"] == ""


def test_local_status_merges_downloaded_and_installed(api: httpx.Client, backend: Backend) -> None:
    _mkzip(backend.data_dir / "downloads" / "2.0" / "Genie_v81.zip", {"a": "b"})
    _mkinstall(backend, "GenieAPIService_v2.0_Genie_v73")
    v = _local(api)["versions"]["2.0"]
    assert v["downloaded"] is True and v["installed"] is True
    assert v["platform_driver"] == "v73", "the installed tag wins over the downloaded one"


def test_local_status_honours_a_custom_download_directory(api: httpx.Client, tmp_path: Path) -> None:
    custom = tmp_path / "mydl"
    _mkzip(custom / "9.0" / "G_v81.zip", {"a": "b"})
    _settings(api, save_dir=str(custom))
    assert _local(api)["versions"]["9.0"]["save_path"] == str(custom / "9.0" / "G_v81.zip")
    assert api.delete(f"{V}/download/9.0").status_code == 200
    assert not (custom / "9.0").exists(), "delete follows the same override"


def test_local_status_with_a_missing_download_directory_is_not_an_error(api: httpx.Client, tmp_path: Path) -> None:
    _settings(api, save_dir=str(tmp_path / "does-not-exist"))
    assert _local(api)["versions"] == {}


def test_local_status_blank_save_dir_means_default(api: httpx.Client, backend: Backend) -> None:
    _mkzip(backend.data_dir / "downloads" / "1.0" / "G.zip", {"a": "b"})
    _settings(api, save_dir="   ")
    assert "1.0" in _local(api)["versions"]


# --------------------------------------------------------------------------
# local-status: the auto-configure side effect
# --------------------------------------------------------------------------


def test_local_status_auto_configures_root_path_to_the_newest_install(api: httpx.Client, backend: Backend) -> None:
    """KNOWN GAP (current behaviour, pinned): a read has a write side effect, and it picks by NUMERIC
    version (2.0 beats 1.0; 10.0 would beat 9.0), not by install time."""
    _mkinstall(backend, "GenieAPIService_v9.0")
    _mkinstall(backend, "GenieAPIService_v10.0")
    _mkinstall(backend, "GenieAPIService_v2.0")
    assert _forge(backend).get("genie_service") is None
    first = _local(api)
    assert first["auto_configured"] is True
    assert first["auto_configured_path"] == str(backend.data_dir / "bin" / "GenieAPIService_v10.0")
    assert _forge(backend)["genie_service"]["root_path"] == first["auto_configured_path"]
    assert _local(api)["auto_configured"] is False, "only the first scan writes"


def test_local_status_does_not_overwrite_an_existing_root_path(api: httpx.Client, backend: Backend) -> None:
    f = _forge_file(backend)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"genie_service": {"root_path": "C:/somewhere/else"}, "security": {"x": 1}}), encoding="utf-8")
    _mkinstall(backend, "GenieAPIService_v1.0")
    assert _local(api)["auto_configured"] is False
    assert _forge(backend)["genie_service"]["root_path"] == "C:/somewhere/else"


def test_auto_configure_preserves_other_sections(api: httpx.Client, backend: Backend) -> None:
    f = _forge_file(backend)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"security": {"x": 1}}), encoding="utf-8")
    _mkinstall(backend, "GenieAPIService_v1.0")
    assert _local(api)["auto_configured"] is True
    assert _forge(backend)["security"] == {"x": 1}


def test_root_path_is_left_dangling_after_the_install_is_deleted(api: httpx.Client, backend: Backend) -> None:
    """KNOWN GAP (current behaviour, pinned): the auto-written pointer is never healed."""
    d = _mkinstall(backend, "GenieAPIService_v2.0")
    _local(api)
    assert _forge(backend)["genie_service"]["root_path"] == str(d)
    assert api.delete(f"{V}/install/2.0").status_code == 200
    again = _local(api)
    assert again["auto_configured"] is False and again["versions"] == {}
    assert _forge(backend)["genie_service"]["root_path"] == str(d) and not d.exists()


# --------------------------------------------------------------------------
# delete installed
# --------------------------------------------------------------------------


def test_delete_installed_version(api: httpx.Client, backend: Backend) -> None:
    d = _mkinstall(backend, "GenieAPIService_v2.3.7_QAIRT_v2.44.0_v73")
    keep = _mkinstall(backend, "GenieAPIService_v2.48.40_GenieService-win-arm64")
    (d / "sub").mkdir()
    (d / "sub" / "f.txt").write_text("x")
    r = api.delete(f"{V}/install/2.3.7")
    assert r.status_code == 200 and r.json() == {"ok": True, "deleted_paths": [str(d)], "version": "2.3.7"}
    assert not d.exists() and keep.exists()
    again = api.delete(f"{V}/install/2.3.7")
    assert again.status_code == 404 and again.json()["code"] == "service_release.download_not_found"


@pytest.mark.parametrize("partial", ["2.3", "3.7", "2", "2.3.", "2.3.7.1"])
def test_delete_installed_needs_a_whole_version_between_separators(api: httpx.Client, backend: Backend, partial: str) -> None:
    d = _mkinstall(backend, "GenieAPIService_v2.3.7_QAIRT_v2.44.0_v73")
    assert api.delete(f"{V}/install/{partial}").status_code == 404
    assert d.exists()


@pytest.mark.parametrize(
    ("what", "left"),
    [
        ("2.44.0", ["GenieAPIService_v2.48.40_GenieService-win-arm64", "other"]),
        ("73", ["GenieAPIService_v2.48.40_GenieService-win-arm64", "other"]),
        ("Genie", ["other"]),
        ("e", []),
    ],
)
def test_delete_installed_matches_substrings_of_the_directory_name(
    api: httpx.Client, backend: Backend, what: str, left: list[str]
) -> None:
    """KNOWN GAP (current behaviour, pinned): the path segment is matched as a substring of the
    directory name, so a QAIRT version, a driver tag or a word deletes installs it does not name."""
    _mkinstall(backend, "GenieAPIService_v2.3.7_QAIRT_v2.44.0_v73")
    _mkinstall(backend, "GenieAPIService_v2.48.40_GenieService-win-arm64")
    _mkinstall(backend, "other")
    assert api.delete(f"{V}/install/{what}").status_code == 200
    assert _bin(backend) == sorted(left)


def test_delete_installed_ignores_backup_dirs_and_removes_dirs_without_the_exe(api: httpx.Client, backend: Backend) -> None:
    _mkinstall(backend, "GenieAPIService_v8.8_x.bak")
    assert api.delete(f"{V}/install/8.8").status_code == 404
    assert _bin(backend) == ["GenieAPIService_v8.8_x.bak"], "the name has a separator after the version, so only the .bak rule protects it"
    _mkinstall(backend, "GenieAPIService_v6.6", exe=False)
    assert api.delete(f"{V}/install/6.6").status_code == 200, "deletion does not require the exe, unlike the scan"
    assert _bin(backend) == ["GenieAPIService_v8.8_x.bak"]


def test_delete_installed_deletes_every_directory_that_matches(api: httpx.Client, backend: Backend) -> None:
    _mkinstall(backend, "GenieAPIService_v2.0_a")
    _mkinstall(backend, "GenieAPIService_v2.0_b")
    r = api.delete(f"{V}/install/2.0")
    assert r.status_code == 200 and len(r.json()["deleted_paths"]) == 2 and _bin(backend) == []


def test_delete_installed_with_stop_running_still_deletes(api: httpx.Client, backend: Backend) -> None:
    """Nothing is running, so the graceful-stop hook is a no-op here; this only proves the flag is accepted."""
    _mkinstall(backend, "GenieAPIService_v2.0")
    assert api.delete(f"{V}/install/2.0", params={"stop_running": "true"}).status_code == 200
    assert _bin(backend) == []


def test_delete_installed_validation(api: httpx.Client) -> None:
    r = api.delete(f"{V}/install/%20")
    assert r.status_code == 400 and r.json()["code"] == "service_release.invalid_download_request"
    assert "query.stop_running" in _field_errors(api.delete(f"{V}/install/abc", params={"stop_running": "maybe"}))
    assert api.delete(f"{V}/install/").status_code in (404, 405)


def test_installed_version_running_probe_shape(api: httpx.Client, backend: Backend) -> None:
    """Only the 'nothing running' answer is testable (see the module docstring on ``wmic``)."""
    _mkinstall(backend, "GenieAPIService_v2.0")
    assert api.get(f"{V}/install/2.0/running").json() == {"version": "2.0", "running": False}
    assert api.get(f"{V}/install/nope/running").json() == {"version": "nope", "running": False}
    assert api.get(f"{V}/install/%20/running").status_code == 400


# --------------------------------------------------------------------------
# delete downloaded
# --------------------------------------------------------------------------


def test_delete_downloaded_removes_zips_and_control_files_and_the_empty_dir(api: httpx.Client, backend: Backend) -> None:
    dl = backend.data_dir / "downloads"
    _mkzip(dl / "7.1" / "G_v73.zip", {"a": "b"})
    (dl / "7.1" / "G_v73.zip.aria2").write_text("partial")
    r = api.delete(f"{V}/download/7.1")
    assert r.status_code == 200 and r.json() == {"ok": True, "deleted_files": [str(dl / "7.1" / "G_v73.zip")], "version": "7.1"}
    assert not (dl / "7.1").exists(), "the .aria2 control file goes too, then the empty folder"


def test_delete_downloaded_keeps_a_folder_with_other_files(api: httpx.Client, backend: Backend) -> None:
    dl = backend.data_dir / "downloads"
    _mkzip(dl / "7.1" / "G.zip", {"a": "b"})
    (dl / "7.1" / "notes.txt").write_text("keep")
    assert api.delete(f"{V}/download/7.1").status_code == 200
    assert (dl / "7.1" / "notes.txt").exists()


def test_delete_downloaded_not_found_cases(api: httpx.Client, backend: Backend) -> None:
    dl = backend.data_dir / "downloads"
    (dl / "7.3").mkdir(parents=True)
    (dl / "7.3" / "readme.txt").write_text("x")
    for v in ("7.0", "7.3"):
        r = api.delete(f"{V}/download/{v}")
        assert r.status_code == 404 and r.json()["code"] == "service_release.download_not_found"
    assert (dl / "7.3" / "readme.txt").exists()
    assert api.delete(f"{V}/download/%20").status_code == 400


def test_delete_downloaded_dot_dot_deletes_zips_outside_the_download_directory(api: httpx.Client, backend: Backend) -> None:
    """KNOWN GAP - SECURITY (current behaviour, pinned): an encoded ``..`` as the version walks out of
    ``downloads/`` and deletes ``*.zip`` files in its parent (here the data root). Nothing validates
    the segment. If this is fixed the stray file survives - update this test then."""
    stray = backend.data_dir / "stray.zip"
    stray.write_bytes(b"not a download")
    (backend.data_dir / "downloads").mkdir(exist_ok=True)
    r = api.delete(f"{V}/download/%2e%2e")
    assert r.status_code == 200
    assert not stray.exists(), "a zip outside the downloads dir was deleted"


def test_delete_downloaded_forward_slash_cannot_leave_the_route(api: httpx.Client, backend: Backend) -> None:
    """An encoded ``/`` never matches the single-segment route, so it cannot be used to traverse."""
    other = backend.data_dir / "downloads" / "keep"
    _mkzip(other / "x.zip", {"a": "b"})
    assert api.delete(f"{V}/download/..%2Fdownloads%2Fkeep").status_code in (404, 405)
    assert (other / "x.zip").exists()


def test_delete_downloaded_backslash_segments_are_honoured(api: httpx.Client, backend: Backend) -> None:
    """KNOWN GAP - SECURITY (current behaviour, pinned): a backslash is not rejected, and Windows
    resolves ``a`` + backslash + ``..`` + backslash + ``keep`` to ``downloads/keep`` - the same
    unchecked-segment problem as ``%2e%2e``, reachable without a literal ``..`` segment. If the
    version is validated this becomes a 4xx."""
    other = backend.data_dir / "downloads" / "keep"
    _mkzip(other / "x.zip", {"a": "b"})
    r = api.delete(f"{V}/download/a%5C..%5Ckeep")
    assert r.status_code == 200 and r.json()["deleted_files"] == [str(backend.data_dir / "downloads" / "a" / ".." / "keep" / "x.zip")]
    assert not (other / "x.zip").exists()
