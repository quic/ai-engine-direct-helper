#!/usr/bin/env python
# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Unified launcher for the ``tests/e2e`` suite: run by feature category, get a report.

Standard library only, so it can be started by any Python 3.9+.  The tests
themselves run in a separate "test-side" interpreter (needs pytest, pytest-asyncio,
httpx, psutil, keyring; websockets for the event-stream tests).

Why one pytest process PER CATEGORY (not one for everything, not in parallel):

* every backend binds port 8989 (and aria2c 6810), so two cannot coexist ->
  strictly serial;
* a category that crashes or hangs (we kill its whole process tree) does not
  take the others down, and each gets its own junit xml + log.

Examples::

    run_e2e.py --list                       # categories, files, test counts
    run_e2e.py                              # everything, serial
    run_e2e.py -c chat,security             # only these categories
    run_e2e.py -x model_builder,cli         # everything except these
    run_e2e.py -c settings -- -k proxy      # args after "--" go to pytest
    run_e2e.py --dry-run                    # show what would run

Output goes to ``<project>/data/e2e-reports/<timestamp>/`` (git-ignored)::

    report.html  summary.json  junit/<category>.xml  logs/<category>.log
    backend-logs/  (only when a test failed: the backend's log + process state at that moment)

Exit code: 0 = no failures/errors (skips are fine), 1 = failures/errors,
2 = usage / pre-flight problem (nothing was run).

The category table below is the ONLY place that maps test files to features.
A ``test_*.py`` that matches no category (or two) aborts the run, so a new test
module cannot silently drop out of the report.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import html
import json
import os
import platform
import re
import socket
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent            # .../tests/e2e
PROJECT = HERE.parent.parent                      # .../tools/qaiappbuilder
PORTS = {8989: "backend", 6810: "aria2c RPC"}     # must be free (see conftest.py)
# Managed apps started by test_app_builder_apps.py; that module SKIPS itself if any of these is busy.
APP_PORTS = {p: "app_builder managed app" for p in (18420, 18421, 18422, 18423, 18555)}
WATCHED = {**PORTS, **APP_PORTS}                  # checked for leftovers after every category

# ---------------------------------------------------------------------------
# Feature categories  (ordered; follows FEATURE-INVENTORY.md section 3)
# name -> (description, [file globs relative to tests/e2e])
# ---------------------------------------------------------------------------
CATEGORIES: dict[str, tuple[str, list[str]]] = {
    "system": ("System & startup (/api/system/*, harness self-tests)",
               ["test_sys_*.py", "test_harness.py"]),
    "auth": ("Login & access control (auth gate, CSRF)",
             ["test_auth_*.py"]),
    "chat": ("Chat, conversations, MCP, OpenAI-compatible API, uploads",
             ["test_chat_*.py", "test_openai_compat*.py", "test_uploads*.py"]),
    "app_builder": ("App Builder (app generation / packaging / runs)",
                    ["test_app_builder*.py"]),
    "model_builder": ("Model Builder (conversion, quantization, QAIRT env)",
                      ["test_model_builder.py", "test_quantization.py", "test_qairt_*.py"]),
    "model_hub": ("Model Hub, downloads, aria2c",
                  ["test_model_hub.py", "test_downloads*.py", "test_aria2c*.py"]),
    "service": ("Local model service & versions",
                ["test_service*.py"]),
    "security": ("Security & permissions (/api/security/*)",
                 ["test_secu_*.py"]),
    "settings": ("Settings, preferences, proxy, TLS verify",
                 ["test_settings.py", "test_set_tls_verify.py"]),
    "skills_tasks": ("Skills and scheduled tasks",
                     ["test_skills*.py", "test_scheduled_tasks.py"]),
    "search": ("Search-engine management (/api/search/engines)",
               ["test_search_engines*.py"]),
    "gallery": ("Gallery submission (local part only)",
                ["test_gallery.py"]),
    "events": ("Event stream (/events, SSE + WebSocket)",
               ["test_events*.py"]),
    "cli": ("qai.bat command line",
            ["test_cli.py"]),
}

# Modules needed on the TEST side, as (import name, pip name).
TEST_SIDE_MODULES = [("pytest", "pytest"), ("pytest_asyncio", "pytest-asyncio"),
                     ("httpx", "httpx"), ("psutil", "psutil"), ("keyring", "keyring"),
                     ("websockets", "websockets")]


# ---------------------------------------------------------------------------
# Category <-> file mapping and self-check
# ---------------------------------------------------------------------------
def all_test_files() -> list[str]:
    return sorted(p.name for p in HERE.glob("test_*.py"))


def files_of(cat: str) -> list[str]:
    pats = CATEGORIES[cat][1]
    return [f for f in all_test_files() if any(fnmatch.fnmatch(f, p) for p in pats)]


def check_mapping() -> list[str]:
    """Return problems: unclassified files, files in two categories, dead patterns."""
    problems: list[str] = []
    owner: dict[str, list[str]] = {}
    for cat in CATEGORIES:
        for f in files_of(cat):
            owner.setdefault(f, []).append(cat)
    for f in all_test_files():
        cats = owner.get(f, [])
        if not cats:
            problems.append(f"{f}: not in any category - add it to CATEGORIES in run_e2e.py")
        elif len(cats) > 1:
            problems.append(f"{f}: matched by several categories {cats}")
    for cat, (_d, pats) in CATEGORIES.items():
        for p in pats:
            if not any(fnmatch.fnmatch(f, p) for f in all_test_files()):
                problems.append(f"category '{cat}': pattern '{p}' matches no file")
    return problems


# ---------------------------------------------------------------------------
# Interpreters
# ---------------------------------------------------------------------------
def _probe(py: str, modules: list[str]) -> dict[str, bool] | None:
    code = ("import importlib.util as u,json;"
            f"print(json.dumps({{m: bool(u.find_spec(m)) for m in {modules!r}}}))")
    try:
        r = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=60)
        return json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def find_test_python(explicit: str | None) -> str | None:
    """--python > $QAI_E2E_TEST_PYTHON > this interpreter > %TEMP%\\qai-e2e-venv."""
    cands = [explicit, os.environ.get("QAI_E2E_TEST_PYTHON"), sys.executable,
             str(Path(os.environ.get("TEMP", "")) / "qai-e2e-venv" / "Scripts" / "python.exe"),
             str(Path(os.environ.get("TMPDIR", "/tmp")) / "qai-e2e-venv" / "bin" / "python")]
    for c in cands:
        if not c or not Path(c).exists():
            continue
        have = _probe(c, ["pytest"])
        if have and have["pytest"]:
            return c
        if c == explicit or c == os.environ.get("QAI_E2E_TEST_PYTHON"):
            return c                      # user asked for it: let preflight say what is missing
    return None


def find_backend_python(explicit: str | None) -> str | None:
    """--backend-python > $QAI_E2E_PYTHON > product venv for this CPU."""
    if explicit:
        return explicit
    if os.environ.get("QAI_E2E_PYTHON"):
        return os.environ["QAI_E2E_PYTHON"]
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "QAIModelBuilder" / "envs"
    arm = platform.machine().lower() in ("arm64", "aarch64")
    for name in ((".venv_arm64_313", ".venv_x64_313") if arm else (".venv_x64_313", ".venv_arm64_313")):
        p = base / name / "Scripts" / "python.exe"
        if p.exists():
            return str(p)
    return None


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def preflight(test_py: str | None, backend_py: str | None, selected: list[str]) -> list[str]:
    errs: list[str] = []
    if not test_py:
        errs.append("no test-side Python with pytest found; pass --python or set QAI_E2E_TEST_PYTHON "
                    "(needs: " + ", ".join(p for _m, p in TEST_SIDE_MODULES) + ")")
    else:
        have = _probe(test_py, [m for m, _p in TEST_SIDE_MODULES])
        if have is None:
            errs.append(f"test Python is not runnable: {test_py}")
        else:
            miss = [p for m, p in TEST_SIDE_MODULES if not have[m]]
            if miss:
                errs.append(f"test Python {test_py} is missing: {', '.join(miss)}  "
                            f"(pip install {' '.join(miss)})")
    if not backend_py:
        errs.append("no backend Python found; set QAI_E2E_PYTHON or --backend-python "
                    "(the product venv, e.g. %LOCALAPPDATA%\\QAIModelBuilder\\envs\\.venv_arm64_313)")
    elif not Path(backend_py).exists():
        errs.append(f"backend Python does not exist: {backend_py}")
    for port, what in PORTS.items():
        if port_busy(port):
            errs.append(f"port {port} ({what}) is already in use - stop whatever owns it; "
                        "the suite aborts rather than killing it")
    if "app_builder" in selected:
        for port in APP_PORTS:
            if port_busy(port):
                o = port_owner(port)
                errs.append(f"port {port} is in use (pid {o['pid']}, {o['cmd'] or 'owner unknown'}) - "
                            "test_app_builder_apps.py would SKIP all its tests; stop it first")
    return errs


# ---------------------------------------------------------------------------
# Running one category
# ---------------------------------------------------------------------------
def kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        proc.kill()


def wait_ports_free(ports, timeout: float = 30.0) -> list[int]:
    ports = list(ports)
    end = time.time() + timeout
    busy = [p for p in ports if port_busy(p)]
    while busy and time.time() < end:
        time.sleep(1.0)
        busy = [p for p in ports if port_busy(p)]
    return busy


def port_owner(port: int) -> dict:
    """Best-effort owner of a listening port (needs psutil in THIS interpreter; else just the port)."""
    info = {"port": port, "what": WATCHED.get(port, "?"), "pid": None, "cmd": "", "cwd": "", "from_pytest": None}
    try:
        import psutil  # optional: the launcher itself stays stdlib-only
        for c in psutil.net_connections("inet"):
            if c.laddr and c.laddr.port == port and c.status == "LISTEN" and c.pid:
                p = psutil.Process(c.pid)
                info["pid"] = c.pid
                info["cmd"] = " ".join(p.cmdline())[:200]
                try:
                    info["cwd"] = p.cwd()
                    info["from_pytest"] = "PYTEST_CURRENT_TEST" in p.environ()
                except Exception:  # noqa: BLE001 - access denied etc.
                    pass
                break
    except Exception:  # noqa: BLE001 - psutil missing or net_connections denied
        pass
    return info


def find_leaks(baseline: set[int], settle: float = 10.0) -> list[dict]:
    """Watched ports that are busy now but were NOT busy when the run started.

    Waits up to ``settle`` seconds first: a stopped server's sockets can take a moment to go away.
    """
    new = [p for p in WATCHED if p not in baseline]
    end = time.time() + settle
    busy = [p for p in new if port_busy(p)]
    while busy and time.time() < end:
        time.sleep(1.0)
        busy = [p for p in new if port_busy(p)]
    return [port_owner(p) for p in busy]


def run_category(cat: str, test_py: str, env: dict, out: Path, extra: list[str],
                 timeout: int, quiet: bool) -> dict:
    junit = out / "junit" / f"{cat}.xml"
    log = out / "logs" / f"{cat}.log"
    files = [str(HERE / f) for f in files_of(cat)]
    cmd = [test_py, "-m", "pytest", *files, f"--junitxml={junit}", *extra]
    res = {"name": cat, "description": CATEGORIES[cat][0], "files": files_of(cat),
           "command": cmd, "returncode": None, "status": "ERROR", "duration": 0.0,
           "note": "", "log": str(log), "junit": str(junit), "leaks": []}

    busy = wait_ports_free(list(PORTS) + (list(APP_PORTS) if cat == "app_builder" else []))
    if busy:
        res["note"] = f"ports still busy before start: {busy}; category not run"
        return res

    t0 = time.time()
    kw = {"start_new_session": True} if os.name != "nt" else \
         {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    proc = subprocess.Popen(cmd, cwd=str(PROJECT), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, **kw)

    def pump() -> None:
        with open(log, "w", encoding="utf-8", errors="replace") as fh:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace")
                fh.write(line)
                if not quiet:
                    sys.stdout.write("    " + line)
                    sys.stdout.flush()

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    try:
        proc.wait(timeout=timeout if timeout > 0 else None)
        res["returncode"] = proc.returncode
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        proc.wait()
        res["returncode"] = None
        res["status"] = "TIMEOUT"
        res["note"] = f"killed after {timeout}s (process tree terminated)"
    th.join(timeout=10)
    res["duration"] = time.time() - t0

    rc = res["returncode"]
    if res["status"] != "TIMEOUT":
        res["status"] = {0: "PASS", 1: "FAIL", 5: "NO TESTS"}.get(rc, "ERROR")
        if res["status"] == "ERROR":
            res["note"] = f"pytest exit code {rc} (2=interrupted, 3=internal error, 4=usage, 130=Ctrl-C)"
    return res


def kill_leak(o: dict) -> str:
    """Kill a leaked listener AND its reloader parents, but only if they carry PYTEST_CURRENT_TEST.

    That variable is inherited from the pytest process, so it proves the process was started by a
    test run; anything without it (e.g. a real product instance) is never touched.
    """
    try:
        import psutil
    except ImportError:
        return "psutil not available in the launcher interpreter; not killed"
    if not o.get("pid"):
        return "owner unknown; not killed"
    try:
        p = psutil.Process(o["pid"])
        if "PYTEST_CURRENT_TEST" not in p.environ():
            return f"pid {p.pid} was not started by a test run; not killed"
        chain = [p]
        while True:
            par = chain[-1].parent()
            if par is None or par.pid == os.getpid():
                break
            try:
                if "PYTEST_CURRENT_TEST" not in par.environ():
                    break
            except Exception:  # noqa: BLE001
                break
            chain.append(par)
        victims = {x for c in chain for x in [c, *c.children(recursive=True)]}
        for v in victims:
            try:
                v.kill()
            except Exception:  # noqa: BLE001
                pass
        psutil.wait_procs(list(victims), timeout=8)
        return f"killed {len(victims)} process(es): " + ", ".join(str(v.pid) for v in sorted(victims, key=lambda v: v.pid))
    except Exception as ex:  # noqa: BLE001
        return f"could not kill: {type(ex).__name__}: {ex}"


# ---------------------------------------------------------------------------
# junit -> results
# ---------------------------------------------------------------------------
def parse_junit(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return []
    cases = []
    for tc in root.iter("testcase"):
        status, msg, detail = "passed", "", ""
        for child, st in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
            node = tc.find(child)
            if node is not None:
                status, msg, detail = st, node.get("message") or "", (node.text or "")
                break
        cases.append({"id": f"{tc.get('classname', '')}::{tc.get('name', '')}",
                      "status": status, "time": float(tc.get("time") or 0),
                      "message": msg, "detail": detail})
    return cases


def tail(path: str, n: int = 40) -> str:
    try:
        return "".join(Path(path).read_text(encoding="utf-8", errors="replace").splitlines(True)[-n:])
    except OSError:
        return ""


def count(cases: list[dict]) -> dict:
    c = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    for x in cases:
        c[x["status"]] += 1
    c["total"] = len(cases)
    return c


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def fmt_dur(s: float) -> str:
    m, sec = divmod(int(round(s)), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m{sec:02d}s" if h else (f"{m}m{sec:02d}s" if m else f"{sec}s")


def write_reports(out: Path, meta: dict, results: list[dict]) -> None:
    for r in results:
        r["cases"] = parse_junit(Path(r["junit"]))
        r["counts"] = count(r["cases"])
        if r["status"] == "PASS" and r["counts"]["total"] and r["counts"]["total"] == r["counts"]["skipped"]:
            r["note"] = (r["note"] + " all tests skipped").strip()
    tot = {k: sum(r["counts"][k] for r in results) for k in ("total", "passed", "failed", "error", "skipped")}
    bad_cat = [r["name"] for r in results if r["status"] not in ("PASS",) or r.get("leaks")]
    leak_cats = [r["name"] for r in results if r.get("leaks")]
    meta = {**meta, "totals": tot, "bad_categories": bad_cat, "leaking_categories": leak_cats,
            "duration": sum(r["duration"] for r in results)}

    # ---- JSON
    jr = []
    for r in results:
        jr.append({k: r[k] for k in ("name", "description", "files", "status", "returncode",
                                     "duration", "note", "counts", "log", "junit", "leaks")} |
                  {"failures": [{"id": c["id"], "status": c["status"], "message": c["message"]}
                                for c in r["cases"] if c["status"] in ("failed", "error")]})
    (out / "summary.json").write_text(json.dumps({"meta": meta, "categories": jr}, indent=2,
                                                 ensure_ascii=False), encoding="utf-8")

    # ---- HTML
    e = html.escape
    color = {"PASS": "#1a7f37", "FAIL": "#cf222e", "ERROR": "#cf222e", "TIMEOUT": "#cf222e",
             "NO TESTS": "#9a6700"}
    rows, details = [], []
    for r in results:
        c = r["counts"]
        rows.append(
            f"<tr><td><a href='#{e(r['name'])}'>{e(r['name'])}</a></td><td>{e(r['description'])}</td>"
            f"<td style='color:{color.get(r['status'], '#000')}'><b>{e(r['status'])}</b>"
            f"{' <b style=color:#cf222e>+LEAK</b>' if r.get('leaks') else ''}</td>"
            f"<td>{c['total']}</td><td>{c['passed']}</td><td>{c['failed']}</td><td>{c['error']}</td>"
            f"<td>{c['skipped']}</td><td>{fmt_dur(r['duration'])}</td></tr>")
        d = [f"<h3 id='{e(r['name'])}'>{e(r['name'])} - <span style='color:{color.get(r['status'], '#000')}'>"
             f"{e(r['status'])}</span></h3>", f"<p>{e(r['description'])}<br>files: "
             f"{e(', '.join(r['files']))}<br>log: <code>{e(r['log'])}</code></p>"]
        if r["note"]:
            d.append(f"<p><b>Note:</b> {e(r['note'])}</p>")
        for lk in r.get("leaks", []):
            d.append(f"<p style='color:#cf222e'><b>LEFTOVER after this category:</b> port {lk['port']} "
                     f"({e(lk['what'])}) still listening - pid {lk['pid']}, <code>{e(lk['cmd'] or 'owner unknown')}</code>"
                     f"{', cwd <code>' + e(lk['cwd']) + '</code>' if lk['cwd'] else ''}"
                     f"{' [started by a test run]' if lk['from_pytest'] else ''}"
                     f"{' - ' + e(lk['action']) if lk.get('action') else ''}</p>")
        bad = [x for x in r["cases"] if x["status"] in ("failed", "error")]
        for x in bad:
            d.append(f"<details open><summary style='color:#cf222e'>{e(x['status'].upper())}: {e(x['id'])} "
                     f"({x['time']:.1f}s)</summary><pre>{e((x['message'] + chr(10) + x['detail'])[:6000])}</pre></details>")
        if not r["cases"] and r["status"] != "PASS":
            d.append(f"<details open><summary>No junit results - log tail</summary><pre>{e(tail(r['log']))}</pre></details>")
        skips: dict[str, int] = {}
        for x in r["cases"]:
            if x["status"] == "skipped":
                skips[x["message"][:160] or "(no reason)"] = skips.get(x["message"][:160] or "(no reason)", 0) + 1
        if skips:
            d.append("<details><summary>Skipped: " + str(sum(skips.values())) + "</summary><ul>" +
                     "".join(f"<li>{n} x {e(m)}</li>" for m, n in sorted(skips.items(), key=lambda kv: -kv[1])) +
                     "</ul></details>")
        details.append("\n".join(d))

    slow = sorted((x | {"cat": r["name"]} for r in results for x in r["cases"]),
                  key=lambda x: -x["time"])[:15]
    slow_html = "".join(f"<tr><td>{x['time']:.1f}s</td><td>{e(x['cat'])}</td><td>{e(x['id'])}</td></tr>"
                        for x in slow)
    verdict = "PASSED" if not tot["failed"] and not tot["error"] and not bad_cat else "FAILED"
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>QAI E2E report</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;max-width:1200px}}
table{{border-collapse:collapse}}td,th{{border:1px solid #d0d7de;padding:4px 10px;text-align:left}}
th{{background:#f6f8fa}}pre{{background:#f6f8fa;padding:8px;overflow:auto;white-space:pre-wrap}}
code{{background:#f6f8fa;padding:1px 4px}}</style></head><body>
<h1>QAI E2E report - <span style="color:{'#1a7f37' if verdict == 'PASSED' else '#cf222e'}">{verdict}</span></h1>
<p>{e(meta['started'])} &middot; platform <b>{e(meta['platform'])}</b> ({e(meta['machine'])}) &middot;
total time {fmt_dur(meta['duration'])}<br>
test python: <code>{e(str(meta['test_python']))}</code> ({e(str(meta['pytest']))})<br>
backend python: <code>{e(str(meta['backend_python']))}</code><br>
git: {e(str(meta['git']))} &middot; real-JWT tests: {'on' if meta['real_jwt'] else 'off (skipped)'} &middot;
extra pytest args: <code>{e(' '.join(meta['pytest_args']) or '-')}</code></p>
<h2>Totals</h2><p>{tot['total']} tests: <b style="color:#1a7f37">{tot['passed']} passed</b>,
<b style="color:#cf222e">{tot['failed']} failed, {tot['error']} errors</b>, {tot['skipped']} skipped</p>
<table><tr><th>Category</th><th>Feature</th><th>Result</th><th>Total</th><th>Pass</th><th>Fail</th>
<th>Error</th><th>Skip</th><th>Time</th></tr>{''.join(rows)}</table>
<h2>Details</h2>{''.join(details)}
<h2>Slowest tests</h2><table><tr><th>Time</th><th>Category</th><th>Test</th></tr>{slow_html}</table>
</body></html>"""
    (out / "report.html").write_text(page, encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def collect_counts(test_py: str, env: dict) -> dict[str, int] | None:
    """Per-file test counts via one ``--collect-only`` (no backend is started)."""
    try:
        r = subprocess.run([test_py, "-m", "pytest", str(HERE), "--collect-only", "-q", "-p", "no:cacheprovider"],
                           cwd=str(PROJECT), env=env, capture_output=True, text=True, timeout=300,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    counts: dict[str, int] = {}
    for line in r.stdout.splitlines():
        m = re.match(r"^(?:.*[\\/])?(test_\w+\.py)::", line)
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts or None


def split_args(argv: list[str]) -> tuple[list[str], list[str]]:
    return (argv[:argv.index("--")], argv[argv.index("--") + 1:]) if "--" in argv else (argv, [])


def main(argv: list[str]) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    own, pytest_args = split_args(argv)
    ap = argparse.ArgumentParser(description="Run tests/e2e by feature category and write a report. "
                                             "Arguments after '--' are passed to pytest.")
    ap.add_argument("-c", "--category", action="append", default=[],
                    help="comma-separated categories to run (default: all)")
    ap.add_argument("-x", "--exclude", action="append", default=[], help="comma-separated categories to skip")
    ap.add_argument("--list", action="store_true", help="list categories, files and test counts, then exit")
    ap.add_argument("--dry-run", action="store_true", help="print the pytest commands, run nothing")
    ap.add_argument("--python", help="test-side Python (default: $QAI_E2E_TEST_PYTHON, this one, %%TEMP%%\\qai-e2e-venv)")
    ap.add_argument("--backend-python", help="Python that runs the backend (default: $QAI_E2E_PYTHON or product venv)")
    ap.add_argument("--real-jwt", action="store_true",
                    help="also run e2e_real_jwt tests (sets QAI_E2E_REAL_JWT=1; consumes real account quota)")
    ap.add_argument("--timeout", type=int, default=5400, help="seconds per category before it is killed (0 = none)")
    ap.add_argument("--keep-leaks", action="store_true",
                    help="report leftover test servers but do not kill them (default: kill, only if they carry PYTEST_CURRENT_TEST)")
    ap.add_argument("--fail-fast", action="store_true", help="stop after the first category that does not pass")
    ap.add_argument("--out", help="report directory (default: data/e2e-reports/<timestamp>)")
    ap.add_argument("-q", "--quiet", action="store_true", help="do not echo pytest output (still logged)")
    args = ap.parse_args(own)

    problems = check_mapping()
    if problems:
        print("Category table is out of date:", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        return 2

    def names(vals: list[str]) -> list[str]:
        return [n.strip() for v in vals for n in v.split(",") if n.strip()]

    wanted, skipped = names(args.category), names(args.exclude)
    unknown = [n for n in wanted + skipped if n not in CATEGORIES]
    if unknown:
        print(f"Unknown category: {', '.join(unknown)}\nValid: {', '.join(CATEGORIES)}", file=sys.stderr)
        return 2
    selected = [c for c in (wanted or CATEGORIES) if c not in skipped]
    if not selected:
        print("Nothing selected.", file=sys.stderr)
        return 2

    test_py = find_test_python(args.python)
    backend_py = find_backend_python(args.backend_python)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    if backend_py:
        env["QAI_E2E_PYTHON"] = backend_py
    if args.real_jwt:
        env["QAI_E2E_REAL_JWT"] = "1"
    else:
        env.pop("QAI_E2E_REAL_JWT", None)

    if args.list:
        cnt = collect_counts(test_py, env) if test_py else None
        print(f"{'category':<14}{'files':>6}{'tests':>7}  description")
        gt = 0
        for cat, (desc, _p) in CATEGORIES.items():
            fs = files_of(cat)
            n = sum(cnt.get(f, 0) for f in fs) if cnt else None
            gt += n or 0
            print(f"{cat:<14}{len(fs):>6}{(n if n is not None else '?'):>7}  {desc}")
            if args.category or args.exclude:
                pass
            for f in fs:
                print(f"{'':<14}{'':>6}{(cnt.get(f, 0) if cnt else '?'):>7}    {f}")
        print(f"{'TOTAL':<14}{len(all_test_files()):>6}{(gt if cnt else '?'):>7}")
        if not cnt:
            print("(test counts unavailable: no usable test-side Python; see --python)")
        return 0

    errs = preflight(test_py, backend_py, selected)
    if errs and not args.dry_run:
        print("Pre-flight failed - nothing was run:", file=sys.stderr)
        for x in errs:
            print("  - " + x, file=sys.stderr)
        return 2

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) if args.out else PROJECT / "data" / "e2e-reports" / stamp
    if args.dry_run:
        for x in errs:
            print("WARNING (would abort a real run): " + x)
        print(f"report dir: {out}")
        for cat in selected:
            print(f"[{cat}] {test_py} -m pytest {' '.join(files_of(cat))} --junitxml=... {' '.join(pytest_args)}")
        return 0
    (out / "junit").mkdir(parents=True, exist_ok=True)
    (out / "logs").mkdir(parents=True, exist_ok=True)

    ver = subprocess.run([test_py, "-m", "pytest", "--version"], capture_output=True, text=True)
    git = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(PROJECT), capture_output=True, text=True)
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "."], cwd=str(PROJECT),
                           capture_output=True, text=True).stdout.strip()
    meta = {"started": _dt.datetime.now().isoformat(timespec="seconds"),
            "platform": platform.system().lower() + "-" + platform.machine().lower(),
            "machine": platform.platform(), "test_python": test_py, "backend_python": backend_py,
            "pytest": (ver.stdout or ver.stderr).strip(),
            "git": (git.stdout.strip() or "?") + (" (+uncommitted changes)" if dirty else ""),
            "real_jwt": args.real_jwt, "pytest_args": pytest_args, "categories": selected}

    print(f"Report dir: {out}\nCategories: {', '.join(selected)}\n")
    env["QAI_E2E_KEEP_DIR"] = str(out / "backend-logs")   # failed tests leave the backend log + process state here
    baseline = {p for p in WATCHED if port_busy(p)}   # busy before we start: not ours, never reported
    results: list[dict] = []
    for i, cat in enumerate(selected, 1):
        print(f"=== [{i}/{len(selected)}] {cat}: {CATEGORIES[cat][0]}  ({len(files_of(cat))} files)")
        r = run_category(cat, test_py, env, out, pytest_args, args.timeout, args.quiet)
        results.append(r)
        r["leaks"] = find_leaks(baseline)
        for lk in r["leaks"]:
            if args.keep_leaks:
                lk["action"] = "left running (--keep-leaks)"
            else:
                lk["action"] = kill_leak(lk)
                time.sleep(1.0)
            print(f"  !! LEFTOVER port {lk['port']} ({lk['what']}): pid {lk['pid']} {lk['cmd'][:90]}  -> {lk['action']}")
        c = count(parse_junit(Path(r["junit"])))
        print(f"=== {cat}: {r['status']}  {c['passed']} passed, {c['failed']} failed, {c['error']} errors, "
              f"{c['skipped']} skipped  ({fmt_dur(r['duration'])})" + (f"  - {r['note']}" if r["note"] else "") + "\n")
        if args.fail_fast and r["status"] != "PASS":
            print("--fail-fast: stopping.")
            break

    meta["categories"] = [r["name"] for r in results]
    write_reports(out, meta, results)
    summ = json.loads((out / "summary.json").read_text(encoding="utf-8"))["meta"]
    t = summ["totals"]
    print(f"Done: {t['total']} tests - {t['passed']} passed, {t['failed']} failed, {t['error']} errors, "
          f"{t['skipped']} skipped in {fmt_dur(summ['duration'])}")
    if summ["bad_categories"]:
        print("Not passing: " + ", ".join(summ["bad_categories"]))
    if summ.get("leaking_categories"):
        print("Left processes behind (ports): " + ", ".join(summ["leaking_categories"]))
    print(f"HTML : {out / 'report.html'}\nJSON : {out / 'summary.json'}")
    kept = sorted((out / "backend-logs").glob("*.txt")) if (out / "backend-logs").is_dir() else []
    if kept:
        print(f"Backend logs kept for {len(kept)} failing test(s): {out / 'backend-logs'}")
    return 1 if (t["failed"] or t["error"] or summ["bad_categories"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
