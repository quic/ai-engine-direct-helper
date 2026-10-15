# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 — settings: user preferences, forge-config, proxy, locale (SET-01 / SET-02).

Endpoints (``interfaces/http/routes/user_prefs.py``): ``/api/preferences``,
``/api/forge-config``, ``/api/proxy``, ``/api/settings/*``.

Isolation (verified): these documents persist to the ``kv_user_prefs`` table in
the backend's throw-away ``<data_dir>/db/qai.db`` (keys ``ui.preferences`` and
``forge.config``). The user's real ``forge_config.json`` was byte-identical
before and after. The proxy password goes to the OS keyring ONLY when a new
non-empty password is supplied; these tests never send one, so the keyring is
not touched.

Observed behaviour worth pinning (it differs from what one might assume):

* ``/api/preferences`` is a 3-field allow-list. ``null`` means "no change";
  ``""`` clears; unknown fields are silently ignored (200, not 400).
* ``/api/forge-config`` POST is a SHALLOW merge: a nested object in the request
  REPLACES the stored one wholesale (it is not deep-merged).
* UI language is a front-end concern. The backend does not store a language and
  its HTTP error envelopes are not localised; locale lives in the message
  catalogs (``qai.platform.i18n``) used by channels/CLI/streaming. The language
  tests therefore verify the catalogs and the normaliser directly rather than
  pretending an HTTP round-trip exists.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator

import httpx
import pytest

pytestmark = pytest.mark.e2e

PREFS = "/api/preferences"
FORGE = "/api/forge-config"
PREF_FIELDS = ("selected_model_id", "selected_model_provider", "selected_service_model")


@pytest.fixture
def api(http: httpx.Client) -> httpx.Client:
    http.get("/api/system/uptime")
    http.headers["X-QAI-CSRF"] = http.cookies.get("qai_csrf")
    return http


@pytest.fixture
def clean_prefs(api: httpx.Client) -> Iterator[None]:
    """There is no DELETE; clearing every field is the documented 'unset'."""
    yield
    api.post(PREFS, json={f: "" for f in PREF_FIELDS})


@pytest.fixture
def probe_key(api: httpx.Client) -> Iterator[str]:
    """A unique top-level forge-config key; set to null afterwards.

    ``null`` is the closest thing to a delete: the key stays present with value
    null (observed), so cleanup is best-effort and the key is ``e2e``-prefixed
    and unique per test so leftovers can never collide.
    """
    key = f"e2e_probe_{uuid.uuid4().hex[:8]}"
    yield key
    api.post(FORGE, json={"config": {key: None}})


# ---------------------------------------------------------------------------
# SET-01  /api/preferences
# ---------------------------------------------------------------------------


def test_set01_preferences_default_to_empty_strings(api, clean_prefs):
    assert api.get(PREFS).json() == {f: "" for f in PREF_FIELDS}


def test_set01_preferences_round_trip_all_fields(api, clean_prefs):
    want = {"selected_model_id": "e2e-m", "selected_model_provider": "e2e-p", "selected_service_model": "e2e-s"}
    r = api.post(PREFS, json=want)
    assert r.status_code == 200 and r.json() == {"status": "saved"}
    assert api.get(PREFS).json() == want


def test_set01_preferences_partial_update_leaves_other_fields(api, clean_prefs):
    api.post(PREFS, json={"selected_model_id": "e2e-m1"})
    api.post(PREFS, json={"selected_model_provider": "e2e-p1"})
    got = api.get(PREFS).json()
    assert got["selected_model_id"] == "e2e-m1"
    assert got["selected_model_provider"] == "e2e-p1"
    assert got["selected_service_model"] == ""


def test_set01_preferences_null_means_no_change(api, clean_prefs):
    api.post(PREFS, json={"selected_model_id": "e2e-keep"})
    api.post(PREFS, json={"selected_model_id": None})
    assert api.get(PREFS).json()["selected_model_id"] == "e2e-keep"


def test_set01_preferences_empty_string_clears_a_field(api, clean_prefs):
    api.post(PREFS, json={"selected_model_id": "e2e-m", "selected_model_provider": "e2e-p"})
    api.post(PREFS, json={"selected_model_id": ""})
    got = api.get(PREFS).json()
    assert got["selected_model_id"] == ""
    assert got["selected_model_provider"] == "e2e-p"  # untouched


def test_set01_preferences_empty_body_changes_nothing(api, clean_prefs):
    api.post(PREFS, json={"selected_model_id": "e2e-m"})
    assert api.post(PREFS, json={}).json() == {"status": "saved"}
    assert api.get(PREFS).json()["selected_model_id"] == "e2e-m"


def test_set01_preferences_unknown_fields_are_ignored_not_stored(api, clean_prefs):
    """Unknown fields are dropped by the request model (``extra='ignore'``).

    COVERAGE GAP, stated honestly: the application layer ALSO filters through
    ``allowed_top_level``. Over HTTP the request model has already removed every
    unknown field, so that second line of defence cannot be exercised here; a
    mutant that disables it survives. Covering it needs a unit test of
    ``SaveDocumentUseCase`` (the existing unit suite is the right home).
    """
    r = api.post(PREFS, json={"selected_model_id": "e2e-m", "bogus_field": "x"})
    assert r.status_code == 200
    got = api.get(PREFS).json()
    assert "bogus_field" not in got
    assert set(got) == set(PREF_FIELDS)


def test_set01_preferences_reject_wrong_types_without_side_effects(api, clean_prefs):
    api.post(PREFS, json={"selected_model_id": "e2e-stable"})
    assert api.post(PREFS, json={"selected_model_id": 123}).status_code == 400
    assert api.get(PREFS).json()["selected_model_id"] == "e2e-stable"


def test_set01_preferences_are_persisted_in_the_database(api, backend, clean_prefs):
    """Settings really land in the temp DB (not only in memory)."""
    import json
    import sqlite3

    api.post(PREFS, json={"selected_model_id": "e2e-persisted"})
    con = sqlite3.connect(
        f"file:{(backend.data_dir / 'db' / 'qai.db').as_posix()}?mode=ro", uri=True, timeout=10
    )
    try:
        row = con.execute("SELECT value_json FROM kv_user_prefs WHERE key='ui.preferences'").fetchone()
    finally:
        con.close()
    assert row is not None
    assert json.loads(row[0])["selected_model_id"] == "e2e-persisted"


# ---------------------------------------------------------------------------
# SET-01  /api/forge-config
# ---------------------------------------------------------------------------


def test_set01_forge_config_has_documented_defaults(api):
    cfg = api.get(FORGE).json()["config"]
    assert {"ai_coding", "auto_title", "security", "service_launch", "ui"} <= set(cfg)
    mods = cfg["ui"]["toolbar_modules"]
    assert mods["model_builder"] == {"enabled": True}
    assert mods["app_builder"] == {"enabled": True}
    # Optional modules ship disabled.
    assert mods["translate"] == {"enabled": False}
    assert mods["ppt"] == {"enabled": False}


def test_set01_forge_config_security_defaults_are_loopback_only(api):
    sec = api.get(FORGE).json()["config"]["security"]
    assert sec["bind_host"] == "127.0.0.1"


def test_set01_forge_config_round_trip_and_defaults_survive(api, probe_key):
    r = api.post(FORGE, json={"config": {probe_key: {"a": 1}}})
    assert r.status_code == 200 and r.json()["status"] == "saved"
    cfg = api.get(FORGE).json()["config"]
    assert cfg[probe_key] == {"a": 1}
    assert "ui" in cfg and "security" in cfg  # defaults not clobbered


def test_set01_forge_config_merge_is_shallow_nested_objects_are_replaced(api, probe_key):
    """CHARACTERISATION: a nested object replaces the stored one; it is not deep-merged.

    A client that wants to change one nested field must send the whole object, or
    it silently drops its siblings. If the merge is ever made deep, update this.
    """
    api.post(FORGE, json={"config": {probe_key: {"a": 1, "b": {"x": 1, "y": 2}}}})
    api.post(FORGE, json={"config": {probe_key: {"b": {"x": 99}}}})
    assert api.get(FORGE).json()["config"][probe_key] == {"b": {"x": 99}}


def test_set01_forge_config_null_stores_null_rather_than_deleting(api, probe_key):
    api.post(FORGE, json={"config": {probe_key: {"a": 1}}})
    api.post(FORGE, json={"config": {probe_key: None}})
    cfg = api.get(FORGE).json()["config"]
    assert probe_key in cfg and cfg[probe_key] is None


def test_set01_forge_config_rejects_a_non_object_config(api):
    assert api.post(FORGE, json={"config": "notadict"}).status_code == 400


def test_set01_forge_config_omitted_config_is_a_harmless_noop(api, probe_key):
    api.post(FORGE, json={"config": {probe_key: {"a": 1}}})
    assert api.post(FORGE, json={}).status_code == 200
    assert api.get(FORGE).json()["config"][probe_key] == {"a": 1}


# ---------------------------------------------------------------------------
# SET-02  proxy
# ---------------------------------------------------------------------------


def test_set02_proxy_defaults_are_empty(api):
    assert api.get("/api/proxy").json() == {"proxy_url": "", "proxy_username": "", "proxy_password": ""}


def test_set02_proxy_username_round_trip_without_touching_the_keyring(api):
    """'****' is the mask meaning 'password unchanged', so no keyring write happens."""
    try:
        r = api.post("/api/proxy", json={"proxy_username": "e2e-user", "proxy_password": "****"})
        assert r.json() == {"success": True}
        got = api.get("/api/proxy").json()
        assert got["proxy_username"] == "e2e-user"
        assert got["proxy_password"] == ""  # never echoed back
    finally:
        api.post("/api/proxy", json={"proxy_username": "", "proxy_password": "****"})


def test_set02_proxy_url_field_in_the_request_is_ignored(api):
    """proxy_url is owned by the runtime-config route; this endpoint drops it."""
    try:
        api.post("/api/proxy", json={"proxy_url": "http://e2e.invalid:1", "proxy_username": "e2e-u", "proxy_password": "****"})
        assert api.get("/api/proxy").json()["proxy_url"] == ""
    finally:
        api.post("/api/proxy", json={"proxy_username": "", "proxy_password": "****"})


def test_set02_saving_the_proxy_removes_a_stale_network_proxy_url(api):
    """An old build stored the URL under forge.config network_proxy.proxy_url, a field
    that is now dead. Saving the proxy must self-heal it away.

    The stale field is seeded through /api/forge-config (the public way to write that
    document) because the proxy GET deliberately reads the URL from elsewhere and so
    cannot reveal it.
    """
    try:
        api.post(FORGE, json={"config": {"network_proxy": {"proxy_url": "http://stale.invalid:9", "proxy_username": ""}}})
        seeded = api.get(FORGE).json()["config"]["network_proxy"]
        assert seeded["proxy_url"] == "http://stale.invalid:9", "could not seed the stale field"

        api.post("/api/proxy", json={"proxy_username": "e2e-heal", "proxy_password": "****"})

        after = api.get(FORGE).json()["config"]["network_proxy"]
        assert "proxy_url" not in after, "stale proxy_url was not cleaned up"
        assert after["proxy_username"] == "e2e-heal"  # and the real data was kept
    finally:
        api.post(FORGE, json={"config": {"network_proxy": None}})
        api.post("/api/proxy", json={"proxy_username": "", "proxy_password": "****"})


# ---------------------------------------------------------------------------
# misc /api/settings/*  - safe defaults on a fresh install
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("chat_hooks", {"hooks": []}),
        ("chat_hooks_enabled", {"enabled": False}),
        ("computer_enabled", {"enabled": False}),
        ("image_cloud", {"format": "webp", "quality": 50}),
        ("subagent_profile_models", {"models": {}}),
    ],
)
def test_set02_settings_endpoints_have_safe_fresh_install_defaults(api, path, expected):
    """Notably: computer-control and chat hooks are OFF until the user opts in."""
    r = api.get(f"/api/settings/{path}")
    assert r.status_code == 200
    assert r.json() == expected


# ---------------------------------------------------------------------------
# SET-01  language: the message catalogs and the normaliser
#
# These import the product package directly (no HTTP): the backend exposes no
# language endpoint, and its HTTP error envelopes are not localised (see the
# module docstring). PYTHONPATH is set by the harness for the backend only, so
# the test process adds it here.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def i18n():
    import sys

    from tests.e2e.conftest import REPO_ROOT

    for p in (str(REPO_ROOT / "src"), str(REPO_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        from qai.platform import i18n as mod
        from qai.platform.i18n.catalogs import CATALOGS
    except ImportError as exc:  # pragma: no cover - dependency of the product venv
        pytest.skip(f"product i18n not importable from the test interpreter: {exc}")
    return mod, CATALOGS


def test_set01_exactly_three_ui_languages_are_supported_and_zh_cn_is_default(i18n):
    mod, catalogs = i18n
    assert tuple(mod.SUPPORTED_UI_LANGUAGES) == ("en", "zh-CN", "zh-TW")
    assert mod.DEFAULT_UI_LANGUAGE == "zh-CN"
    assert set(catalogs) == set(mod.SUPPORTED_UI_LANGUAGES)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("en", "en"),
        ("zh-CN", "zh-CN"),
        ("zh-TW", "zh-TW"),
        ("zh", "zh-CN"),
        ("zh_cn", "zh-CN"),
        ("zh-Hans", "zh-CN"),
        # Anything unrecognised falls back to the default rather than raising.
        ("ja", "zh-CN"),
        ("xx", "zh-CN"),
        ("", "zh-CN"),
        (None, "zh-CN"),
    ],
)
def test_set01_locale_normalisation(i18n, given, expected):
    assert i18n[0].normalize_ui_language(given) == expected


def test_set01_locale_normalisation_is_case_sensitive_for_region_codes(i18n):
    """CHARACTERISATION: 'EN' / 'en-US' / 'ZH-TW' do NOT map to their language.

    The middleware compensates for 'en-US' by trying the language-only tag, but a
    direct caller passing 'EN' silently gets Chinese. Update if made case-folding.
    """
    n = i18n[0].normalize_ui_language
    assert n("EN") == "zh-CN"
    assert n("ZH-TW") == "zh-CN"
    assert n("en-US") == "zh-CN"


def test_set01_all_catalogs_have_the_same_keys(i18n):
    _, catalogs = i18n
    base = set(catalogs["zh-CN"])
    for loc in ("en", "zh-TW"):
        assert set(catalogs[loc]) == base, f"{loc} is missing/extra keys vs zh-CN"


def test_set01_translations_keep_the_same_placeholders(i18n):
    """A placeholder mismatch makes str.format() fail in exactly one language."""
    _, catalogs = i18n
    ph = lambda s: sorted(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", s))  # noqa: E731
    zh = catalogs["zh-CN"]
    for loc in ("en", "zh-TW"):
        bad = [k for k, v in catalogs[loc].items() if ph(v) != ph(zh[k])]
        assert not bad, f"{loc} placeholder mismatch in: {bad[:5]}"


def test_set01_no_translation_is_blank(i18n):
    _, catalogs = i18n
    blank = [(loc, k) for loc, c in catalogs.items() for k, v in c.items() if not str(v).strip()]
    assert not blank


def test_set01_t_resolves_per_locale_and_falls_back_to_the_key(i18n):
    mod, catalogs = i18n
    assert mod.t("_meta.locale", locale="en") == catalogs["en"]["_meta.locale"]
    assert mod.t("_meta.locale", locale="zh-CN") == catalogs["zh-CN"]["_meta.locale"]
    assert mod.t("_meta.locale", locale="en") != mod.t("_meta.locale", locale="zh-CN")
    # A key nobody defined is returned verbatim instead of raising.
    assert mod.t("no.such.key.e2e") == "no.such.key.e2e"
