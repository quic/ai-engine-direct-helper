# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - Model Builder: HTTP auto-export on the FULL backend + the ``qai pack`` CLI (MB-03 / MB-06).

* MB-03: ``POST /api/app-builder/import/auto-export`` and ``/status`` against the
  real backend (real DB, real DI container). ``tests/system/model_builder`` already
  covers the same route on a minimal app with no DB / DI; this adds what only the
  full wiring can break (workspace-root resolution, the out-of-process smoke probe,
  the cross-context bridge).
* MB-06: ``qai pack export | validate | workspace-init`` and ``qai build`` as real
  subprocesses (argv in, exit code + stdout out).

NOT covered (deliberately):

* **A real ``.bin``.** Every ``.bin`` here is synthetic. Whether a genuine context
  binary yields a correct ``manifest.io_contract`` needs the NPU (MB-04, L3).
* **``qai build`` as a conversation.** It is a REPL that streams a cloud LLM; it
  cannot run unattended. Only its argument parsing and its "no cloud provider"
  exit are tested.
* **``DELETE`` / import-commit of the exported packs** - that is the App Builder
  import flow, not Model Builder.

Isolation (verified; every guard below is a hard failure, not a skip):

* Backend: ``QAI_WORKSPACE__MODEL_ROOT`` points the workspace guard at a temp dir.
  The module fixture refuses to continue unless the backend echoes that temp root
  back (probe) - otherwise exports would land in the real ``C:\\WoS_AI``.
* CLI: ``--wos-ai-root`` DEFAULTS to the real ``C:/WoS_AI`` and ``qai build`` opens
  the DB under ``--repo-root/data``. So every CLI call passes ``--wos-ai-root``
  explicitly and runs with ``QAI_DATA__DATA_DIR`` pointing at a temp dir.
* Before/after, the real ``C:\\WoS_AI`` tree is byte-for-byte unchanged.

Observed behaviour worth pinning (it differs from what one might assume):

* ``pack_id_override`` is NOT validated against the kebab-case rule its own DTO
  documents: ``"UPPER"`` is accepted; ``"Not Kebab!"`` is written straight into
  ``manifest.modelId`` and the exported pack still passes ``qai pack validate``.
  (Only ``AppModelId``'s ``[A-Za-z0-9._-]`` rule exists, and it is applied later,
  at import.)
* ``qai pack validate`` is a structural check only: a ``_candidate.json`` with
  ``ready: false`` passes; a manifest with ``variants: []`` passes; an empty
  ``checksum`` disables the weight hash check (a tampered weight then passes).
* Re-exporting with FEWER precisions leaves the dropped precision's ``.bin`` in
  ``app_pack/weights`` (the manifest no longer lists it).
* ``--wos-ai-root`` is the CLI's only path guard and it defaults to the real tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import REPO_ROOT, Backend, _start_backend, _python_executable

pytestmark = pytest.mark.e2e

AUTO = "/api/app-builder/import/auto-export"
STATUS = AUTO + "/status"
MIN_BIN = 1024 * 1024  # == MIN_CONTEXT_BIN_SIZE: smaller .bin files are rejected as stubs
REAL_WOS = Path("C:/WoS_AI")


# --------------------------------------------------------------------------
# isolation plumbing
# --------------------------------------------------------------------------


def _tree_fingerprint(root: Path) -> list[tuple[str, int, int]] | None:
    if not root.exists():
        return None
    return sorted(
        (str(p.relative_to(root)), p.stat().st_mtime_ns, p.stat().st_size) for p in root.rglob("*")
    )


@pytest.fixture(scope="module", autouse=True)
def real_workspace_untouched() -> Iterator[None]:
    """The real ``C:/WoS_AI`` must be identical after this module as before it."""
    before = _tree_fingerprint(REAL_WOS)
    yield
    assert _tree_fingerprint(REAL_WOS) == before, "this module modified the real C:/WoS_AI"


@pytest.fixture(scope="module")
def wos_root() -> Iterator[Path]:
    base = Path(tempfile.mkdtemp(prefix="qai-e2e-mb-"))
    root = base / "WoS_AI"
    root.mkdir()
    yield root
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture(scope="module")
def backend(wos_root: Path) -> Iterator[Backend]:
    """Full backend whose workspace root is the temp ``wos_root`` (proven, not assumed)."""
    mp = pytest.MonkeyPatch()
    mp.setenv("QAI_WORKSPACE__MODEL_ROOT", str(wos_root))
    try:
        gen = _start_backend(auth_enabled=False)
        b = next(gen)
        try:
            with httpx.Client(base_url=b.base_url, timeout=30.0) as c:
                c.get("/api/system/uptime")
                c.headers["X-QAI-CSRF"] = c.cookies.get("qai_csrf")
                probe = c.post(AUTO, json={"source_path": str(wos_root / "__root_probe__")})
            # A path inside the (temp) root reaches "workspace not found" and the message
            # names the root the backend is ACTUALLY using.
            assert probe.status_code == 400, probe.text
            assert str(wos_root) in probe.json()["message"], (
                f"backend is NOT using the temp workspace root; refusing to run. got: {probe.text}"
            )
            yield b
        finally:
            try:
                next(gen)
            except StopIteration:
                pass
    finally:
        mp.undo()


@pytest.fixture
def api(http: httpx.Client) -> httpx.Client:
    http.get("/api/system/uptime")
    http.headers["X-QAI-CSRF"] = http.cookies.get("qai_csrf")
    http.timeout = httpx.Timeout(300.0)  # an export runs a subprocess smoke probe
    return http


@pytest.fixture
def make_ws(wos_root: Path) -> Callable[..., Path]:
    """Build ``<wos_root>/<name>/`` with synthetic artefacts."""

    def _make(
        name: str,
        *,
        precisions: tuple[str, ...] = ("fp16",),
        bin_size: int = MIN_BIN + 16,
        plan: bool = True,
        report: bool = True,
    ) -> Path:
        wd = wos_root / name
        assert not wd.exists(), f"workspace name reused: {name}"
        (wd / "output").mkdir(parents=True)
        if plan:
            (wd / "plan.md").write_text(
                f"# {name}\n## Config\n```\nMODEL_NAME  = {name}\nPRECISION   = {precisions[0]}\n```\n",
                encoding="utf-8",
            )
        if report:
            (wd / "REPORT.md").write_text(
                "# REPORT\n| FP16 | 0.9991 | ok |\nCosine Similarity: 0.9993\nLatency: 12.5 ms\n",
                encoding="utf-8",
            )
        for p in precisions:
            label = {"w8a8": "int8"}.get(p, p)
            (wd / "output" / f"{name}_{label}.bin").write_bytes((p.encode() * 64).ljust(bin_size, b"\0"))
        return wd

    return _make


def _manifest(pack: Path) -> dict:
    return json.loads((pack / "manifest.json").read_text(encoding="utf-8"))


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _only_probe_noise(errors: list[str]) -> None:
    """A synthetic .bin makes the NATIVE smoke probe non-deterministic (measured: ~1 in 4 exports
    its child dies with an access violation and the export degrades to ``io_contract=null`` plus
    this one soft error; otherwise it 'loads' and records an empty contract). Both leave
    ``success=True``. So "no errors" cannot be asserted - but NO OTHER error may appear.
    """
    assert all(e.startswith("io_contract_probe_failed:") for e in errors), errors


# --------------------------------------------------------------------------
# MB-03 - auto-export over HTTP on the full backend
# --------------------------------------------------------------------------


def test_auto_export_produces_a_complete_pack(api: httpx.Client, make_ws: Callable[..., Path]) -> None:
    ws = make_ws("e2e_one")
    assert api.post(STATUS, json={"source_path": str(ws)}).json() == {"status": "idle"}

    r = api.post(AUTO, json={"source_path": str(ws)})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["accepted"] is True and body["success"] is True
    assert body["note"] == "export complete"
    _only_probe_noise(body["errors"])
    assert body["pack_id"] == "e2e-one"  # underscores -> hyphens, lower-cased
    assert body["display_name"] == "E2e One (FP16)"  # title-cased from the workspace name (so 'e2e' -> 'E2e')
    pack = Path(body["source_workdir"])
    assert pack == ws / "app_pack"

    for rel in ("manifest.json", "runner.py", "requirements.txt", "SKILL.md", "_candidate.json",
                "weights/e2e_one_fp16.bin", "provenance/import_meta.json"):
        assert (pack / rel).is_file(), f"missing {rel}"
    # Neither transient marker may outlive the request.
    assert not (pack / ".generating").exists()
    assert api.post(STATUS, json={"source_path": str(ws)}).json() == {"status": "generated"}

    mf = _manifest(pack)
    assert mf["modelId"] == "e2e-one"
    assert [(v["id"], v["default"]) for v in mf["variants"]] == [("fp16", True)]
    v = mf["variants"][0]["assets"]
    exported = pack / "weights" / "e2e_one_fp16.bin"
    assert v["checksum"] == "sha256:" + _sha(exported) == "sha256:" + _sha(ws / "output" / "e2e_one_fp16.bin")
    assert v["sizeBytes"] == exported.stat().st_size
    cand = json.loads((pack / "_candidate.json").read_text(encoding="utf-8"))
    assert cand["ready"] is True and cand["packId"] == "e2e-one"
    assert all(cand["checks"][k] for k in ("plan_parsed", "context_binary_found", "context_binary_size_ok",
                                           "runner_compiles", "manifest_generated"))


def test_exported_weight_is_a_copy_not_a_link(api: httpx.Client, make_ws: Callable[..., Path]) -> None:
    """Deleting the source ``output/`` later must not corrupt the pack."""
    ws = make_ws("e2e_copy")
    assert api.post(AUTO, json={"source_path": str(ws)}).status_code == 202
    src, dst = ws / "output" / "e2e_copy_fp16.bin", ws / "app_pack" / "weights" / "e2e_copy_fp16.bin"
    assert not os.path.samefile(src, dst) and dst.stat().st_nlink == 1


def test_exported_pack_passes_the_cli_validator(
    api: httpx.Client, make_ws: Callable[..., Path], run_qai: Callable[..., "Run"]
) -> None:
    """The HTTP exporter and the CLI validator are separate code paths; they must agree."""
    ws = make_ws("e2e_xval")
    api.post(AUTO, json={"source_path": str(ws)})
    r = run_qai("pack", "validate", str(ws / "app_pack"))
    assert r.rc == 0 and r.out.startswith("[PASS]"), r.out + r.err


def test_auto_export_is_repeatable(api: httpx.Client, make_ws: Callable[..., Path]) -> None:
    ws = make_ws("e2e_rep")
    first = api.post(AUTO, json={"source_path": str(ws)}).json()
    second = api.post(AUTO, json={"source_path": str(ws)}).json()
    assert first["success"] and second["success"]
    assert first["pack_id"] == second["pack_id"]
    _only_probe_noise(first["errors"])
    _only_probe_noise(second["errors"])
    assert api.post(STATUS, json={"source_path": str(ws)}).json() == {"status": "generated"}


def test_overrides_reach_the_manifest(api: httpx.Client, make_ws: Callable[..., Path]) -> None:
    ws = make_ws("e2e_over")
    r = api.post(AUTO, json={
        "source_path": str(ws), "pack_id_override": "my-custom-pack", "display_name_override": "Custom Name",
        "category_override": "ASR", "input_kind_override": "audio", "output_kind_override": "json",
    })
    assert r.status_code == 202 and r.json()["pack_id"] == "my-custom-pack"
    assert r.json()["display_name"] == "Custom Name"
    mf = _manifest(ws / "app_pack")
    assert (mf["modelId"], mf["displayName"], mf["category"]) == ("my-custom-pack", "Custom Name", "ASR")
    assert (mf["inputSchema"]["kind"], mf["outputSchema"]["kind"]) == ("audio", "json")


def test_multi_precision_export(api: httpx.Client, make_ws: Callable[..., Path]) -> None:
    ws = make_ws("e2e_multi", precisions=("fp16", "w8a8"))
    r = api.post(AUTO, json={"source_path": str(ws), "precisions": ["fp16", "int8"], "default_precision": "int8"})
    assert r.status_code == 202 and r.json()["success"] is True
    assert r.json()["display_name"] == "E2e Multi"  # no precision suffix when >1 variant
    mf = _manifest(ws / "app_pack")
    assert [(v["id"], v["default"]) for v in mf["variants"]] == [("fp16", False), ("int8", True)]
    assert sorted(p.name for p in (ws / "app_pack" / "weights").iterdir()) == ["e2e_multi_fp16.bin", "e2e_multi_int8.bin"]
    # The two variants carry DIFFERENT weights, each hashed against its own file.
    sums = {v["id"]: v["assets"]["checksum"] for v in mf["variants"]}
    assert sums["fp16"] != sums["int8"]
    assert sums["int8"] == "sha256:" + _sha(ws / "app_pack" / "weights" / "e2e_multi_int8.bin")


def test_reexport_with_fewer_precisions_leaves_the_dropped_weight_behind(
    api: httpx.Client, make_ws: Callable[..., Path]
) -> None:
    """KNOWN GAP (current behaviour, pinned): a stale ``.bin`` outlives its manifest entry."""
    ws = make_ws("e2e_shrink", precisions=("fp16", "w8a8"))
    api.post(AUTO, json={"source_path": str(ws), "precisions": ["fp16", "int8"]})
    r = api.post(AUTO, json={"source_path": str(ws), "precisions": ["fp16"]})
    assert r.status_code == 202 and r.json()["success"] is True
    assert [v["id"] for v in _manifest(ws / "app_pack")["variants"]] == ["fp16"]
    assert (ws / "app_pack" / "weights" / "e2e_shrink_int8.bin").exists()  # orphaned


@pytest.mark.parametrize("bad", ["Not Kebab!", "UPPER", "has space"])
def test_pack_id_override_is_not_validated(api: httpx.Client, make_ws: Callable[..., Path], bad: str) -> None:
    """KNOWN GAP (current behaviour, pinned): the documented kebab-case rule is not enforced.

    If this starts returning 4xx that is an improvement - update this test.
    """
    ws = make_ws("e2e_id_" + hashlib.sha1(bad.encode()).hexdigest()[:6])
    r = api.post(AUTO, json={"source_path": str(ws), "pack_id_override": bad})
    assert r.status_code == 202 and r.json()["pack_id"] == bad
    assert _manifest(ws / "app_pack")["modelId"] == bad


def test_request_validation(api: httpx.Client) -> None:
    for payload in ({}, {"source_path": ""}, {"source_path": "x" * 5000},
                    {"source_path": "p", "precisions": ["a"] * 17}, {"source_path": "p", "pack_id_override": "a" * 129}):
        r = api.post(AUTO, json=payload)
        assert r.status_code == 400 and r.json()["code"] == "request.validation", payload
    assert api.post(STATUS, json={}).status_code == 400
    assert api.get(AUTO).status_code in (404, 405)


@pytest.mark.parametrize("where", ["parent", "dotdot", "ghost"])
def test_workspace_must_exist_and_be_inside_the_root(api: httpx.Client, wos_root: Path, where: str) -> None:
    path = {"parent": wos_root.parent, "dotdot": wos_root / ".." / "x", "ghost": wos_root / "ghost"}[where]
    r = api.post(AUTO, json={"source_path": str(path)})
    assert r.status_code == 400 and r.json()["code"] == "app_builder.auto_export.invalid_request"
    assert not (Path(path).resolve() / "app_pack").exists()  # a rejected request creates nothing


@pytest.fixture
def outside_workspace(wos_root: Path) -> Iterator[Path]:
    """A COMPLETE, otherwise-exportable workspace that lives next to (not inside) the root.

    This is what makes the path guard testable: with an incomplete workspace the later
    ``output/`` / ``.bin`` checks would also reject it, hiding a removed guard.
    """
    ws = wos_root.parent / "OUTSIDE_ws"
    (ws / "output").mkdir(parents=True)
    (ws / "plan.md").write_text(
        "# p\n## Config\n```\nMODEL_NAME  = outside\nPRECISION   = fp16\n```\n", encoding="utf-8"
    )
    (ws / "output" / "outside_fp16.bin").write_bytes((b"fp16" * 64).ljust(MIN_BIN + 16, b"\0"))
    yield ws
    shutil.rmtree(ws, ignore_errors=True)


def test_a_valid_workspace_outside_the_root_is_refused_and_untouched(
    api: httpx.Client, outside_workspace: Path
) -> None:
    r = api.post(AUTO, json={"source_path": str(outside_workspace)})
    assert r.status_code == 400 and r.json()["code"] == "app_builder.auto_export.invalid_request"
    assert "must be located under" in r.json()["message"]
    assert not (outside_workspace / "app_pack").exists(), "the export wrote outside the workspace root"


@pytest.mark.skipif(sys.platform != "win32", reason="uses a directory junction")
def test_a_junction_inside_the_root_cannot_escape_it(
    api: httpx.Client, wos_root: Path, outside_workspace: Path
) -> None:
    link = wos_root / "e2e_junction"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside_workspace)],
                          capture_output=True, text=True)
    if made.returncode != 0:
        pytest.fail(f"could not create the junction needed for this test: {made.stdout} {made.stderr}")
    try:
        r = api.post(AUTO, json={"source_path": str(link)})
        assert r.status_code == 400 and "must be located under" in r.json()["message"]
        assert not (outside_workspace / "app_pack").exists()
    finally:
        os.rmdir(link)  # removes only the junction, never its target


def test_invalid_default_precision_is_400(api: httpx.Client, make_ws: Callable[..., Path]) -> None:
    ws = make_ws("e2e_baddef")
    r = api.post(AUTO, json={"source_path": str(ws), "precisions": ["fp16"], "default_precision": "w8a16"})
    assert r.status_code == 400 and "not in precisions" in r.json()["message"]


@pytest.mark.parametrize(
    ("tag", "kwargs", "expect_in_message"),
    [
        ("stub", {"bin_size": 10}, "too small"),
        ("nobin", {"precisions": ()}, "no .bin/.dlc files"),
    ],
)
def test_unusable_weight_is_409_with_diagnostics(
    api: httpx.Client, make_ws: Callable[..., Path], wos_root: Path, tag: str, kwargs: dict, expect_in_message: str
) -> None:
    if tag == "nobin":
        ws = wos_root / "e2e_nobin"
        (ws / "output").mkdir(parents=True)
        (ws / "plan.md").write_text("# p\n", encoding="utf-8")
    else:
        ws = make_ws("e2e_" + tag, **kwargs)
    r = api.post(AUTO, json={"source_path": str(ws)})
    assert r.status_code == 409, r.text
    j = r.json()
    assert j["code"] == "app_builder.auto_export.missing_context_bin"
    assert expect_in_message in j["message"]
    diag = j["details"]["diagnostics"]
    assert Path(diag["root"]) == ws / "output" and diag["root_exists"] is True
    if tag == "stub":
        assert [(Path(x["path"]).name, x["size_bytes"]) for x in diag["rejected"]] == [("e2e_stub_fp16.bin", 256)]


def test_unknown_precision_token_is_409(api: httpx.Client, make_ws: Callable[..., Path]) -> None:
    ws = make_ws("e2e_unkprec")
    r = api.post(AUTO, json={"source_path": str(ws), "precisions": ["fp99"]})
    assert r.status_code == 409 and r.json()["code"] == "app_builder.auto_export.missing_context_bin"


@pytest.mark.parametrize(
    ("tag", "kwargs", "payload"),
    [
        ("stub", {"bin_size": 10}, {}),
        ("baddef", {}, {"precisions": ["fp16"], "default_precision": "w8a16"}),
        ("unkprec", {}, {"precisions": ["fp99"]}),
    ],
)
def test_failed_export_leaves_no_trace(
    api: httpx.Client, make_ws: Callable[..., Path], tag: str, kwargs: dict, payload: dict
) -> None:
    """A failed export must not leave the panel stuck on 'generating' or a half-written pack."""
    ws = make_ws("e2e_fail_" + tag, **kwargs)
    assert api.post(AUTO, json={"source_path": str(ws), **payload}).status_code in (400, 409)
    assert api.post(STATUS, json={"source_path": str(ws)}).json() == {"status": "idle"}
    app_pack = ws / "app_pack"
    assert not app_pack.exists() or list(app_pack.rglob("*")) == []


def test_workspace_without_plan_or_report_still_exports(api: httpx.Client, make_ws: Callable[..., Path]) -> None:
    ws = make_ws("e2e_bare", plan=False, report=False)
    r = api.post(AUTO, json={"source_path": str(ws)})
    assert r.status_code == 202 and r.json()["success"] is True and r.json()["pack_id"] == "e2e-bare"
    assert (ws / "app_pack" / "provenance" / "source_REPORT.md").is_file()  # placeholder is created


def test_garbage_bin_is_accepted_when_the_probe_cannot_reject_it(
    api: httpx.Client, make_ws: Callable[..., Path]
) -> None:
    """KNOWN GAP (current behaviour, pinned): a non-QNN file of sufficient size exports 'successfully'.

    Measured on this host (8 runs each): the smoke probe either 'loads' the junk and records an
    EMPTY io_contract that is nevertheless marked ``validated_at_export: true``, or its child
    dies with an access violation and the export degrades to ``io_contract = null`` plus one
    soft error. In neither case does the export fail, so ``success`` alone does not prove the
    .bin is a valid context binary. Real validation needs the NPU (MB-04).
    """
    ws = make_ws("e2e_junk")
    (ws / "output" / "e2e_junk_fp16.bin").write_bytes(b"not a qnn context binary " * 50000)
    r = api.post(AUTO, json={"source_path": str(ws)})
    assert r.status_code == 202 and r.json()["success"] is True
    _only_probe_noise(r.json()["errors"])
    contract = _manifest(ws / "app_pack").get("io_contract")
    assert contract is None or (contract["inputs"] == [] and contract["outputs"] == []), contract
    # The service must survive a crashing probe child.
    assert api.get("/api/system/health").status_code == 200


def test_status_generating_and_stale_sentinel(api: httpx.Client, make_ws: Callable[..., Path]) -> None:
    ws = make_ws("e2e_stat")
    (ws / "app_pack").mkdir()
    sentinel = ws / "app_pack" / ".generating"
    sentinel.write_text("x")
    assert api.post(STATUS, json={"source_path": str(ws)}).json() == {"status": "generating"}
    old = sentinel.stat().st_mtime - 3 * 3600  # older than the 1h staleness window
    os.utime(sentinel, (old, old))
    assert api.post(STATUS, json={"source_path": str(ws)}).json() == {"status": "idle"}
    (ws / "app_pack" / "_candidate.json").write_text("{}")
    assert api.post(STATUS, json={"source_path": str(ws)}).json() == {"status": "generated"}  # candidate wins


def test_status_of_a_missing_workspace_is_idle(api: httpx.Client, wos_root: Path) -> None:
    assert api.post(STATUS, json={"source_path": str(wos_root / "ghost")}).json() == {"status": "idle"}


# --------------------------------------------------------------------------
# MB-06 - the `qai` CLI
# --------------------------------------------------------------------------


class Run:
    def __init__(self, p: subprocess.CompletedProcess[str]) -> None:
        self.rc, self.out, self.err = p.returncode, p.stdout.strip(), p.stderr.strip()


@pytest.fixture(scope="module")
def cli_data_dir() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(prefix="qai-e2e-cli-data-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="module")
def run_qai(cli_data_dir: Path) -> Callable[..., Run]:
    """Run ``python -m apps.cli ...`` with the DB redirected to a temp data dir."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)])
    env["PYTHONIOENCODING"] = "utf-8"
    env["QAI_DATA__DATA_DIR"] = str(cli_data_dir)

    def _run(*args: str, timeout: float = 300.0) -> Run:
        return Run(subprocess.run(
            [_python_executable(), "-m", "apps.cli", *args], cwd=str(REPO_ROOT), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=timeout,
        ))

    return _run


def test_cli_data_dir_is_actually_isolated(run_qai: Callable[..., Run], cli_data_dir: Path) -> None:
    """Guard for every CLI test below: prove the CLI honours QAI_DATA__DATA_DIR before it writes."""
    r = run_qai("pack", "list")
    assert r.rc == 0, r.err[-800:]
    assert (cli_data_dir / "db" / "qai.db").is_file(), "CLI did not use the temp data dir"


def test_cli_usage_errors_exit_2(run_qai: Callable[..., Run]) -> None:
    assert run_qai("nosuchcmd").rc == 2
    assert run_qai("pack", "export").rc == 2  # --workdir is required
    assert run_qai("pack", "validate").rc == 2  # pack_dir is required
    assert run_qai("pack", "workspace-init", "  ", "--wos-ai-root", "x").rc == 2  # blank name
    assert run_qai("build", "--mode", "bogus").rc == 2


def test_cli_workspace_init(run_qai: Callable[..., Run], wos_root: Path) -> None:
    r = run_qai("pack", "workspace-init", "cli_init", "--precision", "w8a8", "--wos-ai-root", str(wos_root))
    assert r.rc == 0 and r.out == f"[OK] Workspace initialized: {wos_root / 'cli_init'}"
    wd = wos_root / "cli_init"
    assert (wd / "output").is_dir()
    plan = (wd / "plan.md").read_text(encoding="utf-8")
    assert "MODEL_NAME    = cli_init" in plan and "PRECISION     = w8a8" in plan
    assert str(wd / "output") in plan

    # Idempotent and never clobbers the user's plan.
    (wd / "plan.md").write_text("MY EDITS", encoding="utf-8")
    assert run_qai("pack", "workspace-init", "cli_init", "--precision", "fp16", "--wos-ai-root", str(wos_root)).rc == 0
    assert (wd / "plan.md").read_text(encoding="utf-8") == "MY EDITS"


def test_cli_workspace_init_refuses_to_leave_the_root(run_qai: Callable[..., Run], wos_root: Path) -> None:
    for name in ("../cli_escape", ".."):
        r = run_qai("pack", "workspace-init", name, "--wos-ai-root", str(wos_root))
        assert r.rc == 1 and "workdir must be under" in r.err, name
    assert not (wos_root.parent / "cli_escape").exists()


def test_cli_export_then_validate(run_qai: Callable[..., Run], make_ws: Callable[..., Path], wos_root: Path) -> None:
    ws = make_ws("cli_exp")
    r = run_qai("pack", "export", "--workdir", str(ws), "--wos-ai-root", str(wos_root))
    assert r.rc == 0, r.out + r.err[-800:]
    assert r.out.splitlines()[-1] == f"[OK] Pack ready: {ws / 'app_pack'}"
    assert "[INFO] Candidate ready: True" in r.out
    mf = _manifest(ws / "app_pack")
    assert mf["modelId"] == "cli-exp" and [v["id"] for v in mf["variants"]] == ["fp16"]

    v = run_qai("pack", "validate", str(ws / "app_pack"))
    assert v.rc == 0 and v.out == f"[PASS] {ws / 'app_pack'}"


def test_cli_export_is_deterministic_apart_from_timestamps_and_paths(
    run_qai: Callable[..., Run], make_ws: Callable[..., Path], wos_root: Path
) -> None:
    ws = make_ws("cli_det")
    args = ("pack", "export", "--workdir", str(ws), "--wos-ai-root", str(wos_root))

    def snapshot() -> dict[str, str]:
        return {p.relative_to(ws / "app_pack").as_posix(): _sha(p) for p in (ws / "app_pack").rglob("*") if p.is_file()}

    assert run_qai(*args).rc == 0
    a, ma = snapshot(), _manifest(ws / "app_pack")
    assert run_qai(*args).rc == 0
    b, mb = snapshot(), _manifest(ws / "app_pack")
    assert set(a) == set(b)
    # Only generated-at style files may differ between two runs; the weights, SKILL, requirements,
    # and every manifest field except provenance must be identical.
    assert {k for k in a if a[k] != b[k]} <= {"_candidate.json", "manifest.json", "runner.py",
                                              "provenance/import_meta.json"}
    assert {k for k in ma if ma[k] != mb[k]} <= {"provenance", "io_contract"}  # io_contract: see _only_probe_noise
    assert a["weights/cli_det_fp16.bin"] == b["weights/cli_det_fp16.bin"]


def test_cli_export_flags(run_qai: Callable[..., Run], make_ws: Callable[..., Path], wos_root: Path) -> None:
    ws = make_ws("cli_multi", precisions=("fp16", "w8a8"))
    r = run_qai("pack", "export", "--workdir", str(ws), "--wos-ai-root", str(wos_root),
                "--precisions", "fp16", "--precision", "int8",  # --precision is MERGED into --precisions
                "--default-precision", "int8", "--pack-id", "cli-multi", "--display-name", "CLI Multi")
    assert r.rc == 0, r.out + r.err[-600:]
    mf = _manifest(ws / "app_pack")
    assert (mf["modelId"], mf["displayName"]) == ("cli-multi", "CLI Multi")
    assert [(v["id"], v["default"]) for v in mf["variants"]] == [("fp16", False), ("int8", True)]


def test_cli_export_failures_exit_1_and_leave_nothing(
    run_qai: Callable[..., Run], make_ws: Callable[..., Path], wos_root: Path
) -> None:
    root = ("--wos-ai-root", str(wos_root))
    stub = make_ws("cli_stub", bin_size=10)
    r = run_qai("pack", "export", "--workdir", str(stub), *root)
    assert r.rc == 1 and "too small" in r.err
    assert not (stub / "app_pack").exists() or list((stub / "app_pack").rglob("*")) == []

    bad = make_ws("cli_baddef", precisions=("fp16", "w8a8"))
    r = run_qai("pack", "export", "--workdir", str(bad), *root, "--precisions", "fp16", "int8",
                "--default-precision", "w8a16")
    assert r.rc == 1 and "is not in precisions" in r.err

    assert run_qai("pack", "export", "--workdir", str(wos_root / "ghost"), *root).rc == 1
    assert run_qai("pack", "export", "--workdir", str(wos_root.parent), *root).rc == 1  # outside the root


def test_cli_export_refuses_a_valid_workspace_outside_the_root(
    run_qai: Callable[..., Run], outside_workspace: Path, wos_root: Path
) -> None:
    r = run_qai("pack", "export", "--workdir", str(outside_workspace), "--wos-ai-root", str(wos_root))
    assert r.rc == 1 and "must be located under" in r.err
    assert not (outside_workspace / "app_pack").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="uses a directory junction")
def test_a_workspace_root_reached_through_a_junction_still_works(
    run_qai: Callable[..., Run], tmp_path: Path
) -> None:
    """The guard resolves BOTH sides. If it resolved only the workdir, a root configured through a
    junction (a normal way to put WoS_AI on another drive) would reject every valid workspace.
    """
    real = tmp_path / "real_root"
    (real / "jx" / "output").mkdir(parents=True)
    (real / "jx" / "plan.md").write_text(
        "# p\n## Config\n```\nMODEL_NAME  = jx\nPRECISION   = fp16\n```\n", encoding="utf-8"
    )
    (real / "jx" / "output" / "jx_fp16.bin").write_bytes((b"fp16" * 64).ljust(MIN_BIN + 16, b"\0"))
    link = tmp_path / "root_via_junction"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)], capture_output=True, text=True)
    if made.returncode != 0:
        pytest.fail(f"could not create the junction needed for this test: {made.stdout} {made.stderr}")
    try:
        r = run_qai("pack", "export", "--workdir", str(link / "jx"), "--wos-ai-root", str(link))
        assert r.rc == 0, r.out + r.err[-600:]
        assert (real / "jx" / "app_pack" / "manifest.json").is_file()
        r = run_qai("pack", "workspace-init", "jx2", "--wos-ai-root", str(link))
        assert r.rc == 0 and (real / "jx2" / "plan.md").is_file()
    finally:
        os.rmdir(link)  # removes only the junction, never its target


def test_cli_export_succeeds_without_plan_md(run_qai: Callable[..., Run], wos_root: Path) -> None:
    """Pinned: ``plan.md`` is optional (the workspace name is used). So the CLI's "exit 1 on
    ``result.success == False``" branch is UNREACHABLE from outside: every check that feeds
    ``success`` (plan_parsed, runner_compiles, manifest_generated, size ok) is true for any
    workspace that gets past the ``.bin`` lookup, and a missing/stub .bin raises instead.
    That branch is therefore not covered by any black-box test.
    """
    ws = wos_root / "cli_noplan"
    (ws / "output").mkdir(parents=True)
    (ws / "output" / "cli_noplan_fp16.bin").write_bytes((b"x" * 64).ljust(MIN_BIN + 16, b"\0"))
    r = run_qai("pack", "export", "--workdir", str(ws), "--wos-ai-root", str(wos_root))
    assert r.rc == 0 and "[INFO] Candidate ready: True" in r.out


@pytest.fixture(scope="module")
def good_pack(run_qai: Callable[..., Run], wos_root: Path) -> Path:
    """One exported pack, copied per case by the validate tests so they never mutate it."""
    ws = wos_root / "cli_good"
    (ws / "output").mkdir(parents=True)
    (ws / "plan.md").write_text("# p\n## Config\n```\nMODEL_NAME  = cli_good\nPRECISION   = fp16\n```\n", encoding="utf-8")
    (ws / "output" / "cli_good_fp16.bin").write_bytes((b"fp16" * 64).ljust(MIN_BIN + 16, b"\0"))
    assert run_qai("pack", "export", "--workdir", str(ws), "--wos-ai-root", str(wos_root)).rc == 0
    return ws / "app_pack"


def _edit_manifest(d: Path, fn: Callable[[dict], None]) -> None:
    m = _manifest(d)
    fn(m)
    (d / "manifest.json").write_text(json.dumps(m), encoding="utf-8")


def _tamper_weight(d: Path) -> None:
    p = next((d / "weights").glob("*.bin"))
    b = bytearray(p.read_bytes())
    b[100] ^= 0xFF
    p.write_bytes(bytes(b))


@pytest.mark.parametrize(
    ("tag", "mutate", "needle"),
    [
        ("no_manifest", lambda d: (d / "manifest.json").unlink(), "manifest.json missing"),
        ("manifest_not_json", lambda d: (d / "manifest.json").write_text("{nope", encoding="utf-8"), "manifest.json unreadable"),
        ("no_modelId", lambda d: _edit_manifest(d, lambda m: m.pop("modelId")), "missing field 'modelId'"),
        ("no_runtime", lambda d: _edit_manifest(d, lambda m: m.pop("runtime")), "missing field 'runtime'"),
        ("no_runner", lambda d: (d / "runner.py").unlink(), "runner.py missing"),
        ("runner_syntax", lambda d: (d / "runner.py").write_text("def (:\n", encoding="utf-8"), "runner.py does not compile"),
        ("no_requirements", lambda d: (d / "requirements.txt").unlink(), "requirements.txt missing"),
        ("no_weights", lambda d: shutil.rmtree(d / "weights"), "weights file not found"),
        ("tampered_weight", _tamper_weight, "SHA-256 mismatch"),
        ("candidate_not_json", lambda d: (d / "_candidate.json").write_text("{nope", encoding="utf-8"), "_candidate.json unreadable"),
        ("no_candidate", lambda d: (d / "_candidate.json").unlink(), "_candidate.json missing"),
    ],
)
def test_cli_validate_rejects_broken_packs(
    run_qai: Callable[..., Run], good_pack: Path, tmp_path: Path, tag: str, mutate: Callable[[Path], None], needle: str
) -> None:
    d = tmp_path / tag
    shutil.copytree(good_pack, d)
    assert run_qai("pack", "validate", str(d)).rc == 0, "control: the unmodified copy must pass"
    mutate(d)
    r = run_qai("pack", "validate", str(d))
    assert r.rc == 1 and r.out.startswith(f"[FAIL] {d}"), r.out + r.err
    assert needle in r.out


def test_cli_validate_reports_every_problem_not_just_the_first(run_qai: Callable[..., Run], tmp_path: Path) -> None:
    d = tmp_path / "empty"
    d.mkdir()
    r = run_qai("pack", "validate", str(d))
    assert r.rc == 1
    for needle in ("manifest.json missing", "runner.py missing", "requirements.txt missing", "_candidate.json missing"):
        assert needle in r.out
    r = run_qai("pack", "validate", str(tmp_path / "ghost"))
    assert r.rc == 1 and "pack directory not found" in r.out


@pytest.mark.parametrize(
    ("tag", "mutate"),
    [
        ("ready_false", lambda d: (d / "_candidate.json").write_text(
            json.dumps({**json.loads((d / "_candidate.json").read_text(encoding="utf-8")), "ready": False}), encoding="utf-8")),
        ("variants_emptied", lambda d: _edit_manifest(d, lambda m: m.update(variants=[]))),
        ("checksum_blanked_then_tampered", lambda d: (
            _edit_manifest(d, lambda m: [v["assets"].update(checksum="") for v in m["variants"]]), _tamper_weight(d))),
    ],
)
def test_cli_validate_known_blind_spots(
    run_qai: Callable[..., Run], good_pack: Path, tmp_path: Path, tag: str, mutate: Callable[[Path], None]
) -> None:
    """KNOWN GAP (current behaviour, pinned): ``validate`` is structural only and passes these.

    If any of these starts failing validation that is an improvement - update this test.
    """
    d = tmp_path / tag
    shutil.copytree(good_pack, d)
    mutate(d)
    r = run_qai("pack", "validate", str(d))
    assert r.rc == 0 and r.out == f"[PASS] {d}", r.out


def test_cli_build_without_a_cloud_provider_exits_1_and_writes_nothing(
    run_qai: Callable[..., Run], cli_data_dir: Path
) -> None:
    """``qai build`` must fail fast (no hang on stdin) and must not create a conversation."""
    import sqlite3

    run_qai("pack", "list")  # make sure the temp DB exists and is migrated
    con = sqlite3.connect(str(cli_data_dir / "db" / "qai.db"), timeout=15)
    try:
        before = con.execute("SELECT COUNT(*) FROM chat_conversation").fetchone()[0]
        r = run_qai("build", timeout=120)
        after = con.execute("SELECT COUNT(*) FROM chat_conversation").fetchone()[0]
    finally:
        con.close()
    assert r.rc == 1 and "qai config setup" in r.err
    assert before == after, "a failed `qai build` left a conversation behind"
