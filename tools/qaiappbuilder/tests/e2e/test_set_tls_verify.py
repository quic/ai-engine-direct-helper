# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - the ``ssl_verify`` runtime switch takes effect without a restart (SET-03).

``PUT /api/security/runtime-config {"ssl_verify": false}`` is documented as hot: outbound HTTPS clients read the flag per request. SECU-04
could only check that the value is stored (no TLS peer to observe). Here there is one: a local TLS server with a SELF-SIGNED certificate
fronts the scripted stand-in LLM (``_fake_llm.py``). The chat's cloud path (the host ``localhost.``, which the product does not treat as
loopback) is pointed at it, so a chat turn is a real HTTPS request whose certificate verification either passes or fails.

* verify ON  -> the handshake is refused: the turn ends with ``chat.llm.tls_cert_untrusted`` and the stand-in sees NO request;
* verify OFF -> the same running backend answers normally (``ECHO: ...``) and the stand-in sees the request;
* back ON    -> refused again.

The certificate is minted in a child process under the PRODUCT Python (the test venv has no ``cryptography``); nothing is installed.

NOT covered (deliberately):

* Every other outbound client that honours the flag (model download, MCP over HTTPS, channel WebSockets, update checks): they need their own TLS
  peers. Only the chat LLM path is observed.
* A real CA-signed certificate, hostname mismatches (the cert is valid for ``localhost.``), expired certificates, and the
  ``REQUESTS_CA_BUNDLE`` / ``SSL_CERT_FILE`` environment variables.
* Persisting the flag across a restart (that is SECU-04).

Isolation (verified): the key and certificate live in a temp folder that is deleted afterwards; the TLS server binds 127.0.0.1 on an ephemeral
port and forwards every connection to the stand-in; the backend is a throw-away one; nothing leaves the machine. The setting is restored to
``true`` at the end of every test.
"""

from __future__ import annotations

import os
import shutil
import socket
import socketserver
import ssl
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests.e2e import _fake_llm
from tests.e2e._fake_llm import StandInLLM
from tests.e2e._owned_backend import owned_backend
from tests.e2e.test_chat_stream import _set_env, run_turn

pytestmark = pytest.mark.e2e

SEC = "/api/security"
MINT = r'''
import datetime, ipaddress, sys
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
out = sys.argv[1]
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "e2e-tls.invalid")])
now = datetime.datetime.now(datetime.timezone.utc)
cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost."), x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256()))
open(out + "/cert.pem", "wb").write(cert.public_bytes(serialization.Encoding.PEM))
open(out + "/key.pem", "wb").write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
'''


class TlsFront:
    """A TLS socket server that terminates TLS with a self-signed certificate and forwards the plaintext to ``upstream_port``."""

    def __init__(self, certdir: Path, upstream_port: int) -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(certdir / "cert.pem"), str(certdir / "key.pem"))
        self.handshakes_ok = 0
        self.handshakes_failed = 0
        front = self

        class Pipe(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                try:
                    tls = ctx.wrap_socket(self.request, server_side=True)
                except (ssl.SSLError, OSError):
                    front.handshakes_failed += 1
                    return
                front.handshakes_ok += 1
                up = socket.create_connection(("127.0.0.1", upstream_port))

                def pump(src: socket.socket, dst: socket.socket) -> None:
                    try:
                        while data := src.recv(65536):
                            dst.sendall(data)
                    except OSError:
                        pass
                    finally:
                        try:
                            dst.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass

                t = threading.Thread(target=pump, args=(up, tls), daemon=True)
                t.start()
                pump(tls, up)
                t.join(5)
                up.close()

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Pipe)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def reset(self) -> None:
        self.handshakes_ok = self.handshakes_failed = 0

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(scope="module")
def tls() -> Iterator[tuple[StandInLLM, TlsFront]]:
    certdir = Path(tempfile.mkdtemp(prefix="qai-e2e-tls-"))
    llm = _fake_llm.start()
    front = None
    try:
        py = os.environ.get("QAI_E2E_PYTHON") or ""
        from tests.e2e.conftest import _python_executable

        r = subprocess.run([_python_executable(), "-W", "ignore", "-c", MINT, str(certdir)], capture_output=True, text=True, timeout=120)
        assert r.returncode == 0 and (certdir / "cert.pem").is_file(), r.stderr[-400:]
        front = TlsFront(certdir, int(llm.base_url.split(":")[2].split("/")[0]))
        yield llm, front
    finally:
        if front:
            front.stop()
        llm.stop()
        shutil.rmtree(certdir, ignore_errors=True)


@pytest.fixture(scope="module")
def backend(tls: tuple) -> Iterator[httpx.Client]:
    llm, front = tls
    old = _set_env({"QAI_CHAT__LLM_BASE_URL": f"https://localhost.:{front.port}/v1", "QAI_CHAT__LLM_DEFAULT_MODEL": "e2e-model", "QAI_CHAT__LLM_API_KEY": "e2e-key"})
    try:
        with owned_backend() as ob:
            cl = ob.start()
            yield cl
    finally:
        _set_env(old)


@pytest.fixture(autouse=True)
def verify_on_again(backend: httpx.Client, tls: tuple) -> Iterator[None]:
    llm, front = tls
    llm.clear()
    front.reset()
    yield
    backend.put(f"{SEC}/runtime-config", json={"ssl_verify": True})


def _turn(cl: httpx.Client, tag: str):
    cid = cl.post("/api/chat/conversations", json={"title": "e2e"}).json()["id"]
    return run_turn(cl, cid, tag, "[echo] over tls", seconds=60)


def _set_verify(cl: httpx.Client, value: bool) -> httpx.Response:
    r = cl.put(f"{SEC}/runtime-config", json={"ssl_verify": value})
    assert r.status_code == 200, r.text
    return r


def test_verification_is_on_by_default_and_a_self_signed_certificate_is_refused(backend: httpx.Client, tls: tuple) -> None:
    llm, front = tls
    assert backend.get(f"{SEC}/runtime-config").json()["ssl_verify"] is True
    t = _turn(backend, "tls-on")
    assert t.text == "" and [e["code"] for e in t.errors] == ["chat.llm.tls_cert_untrusted"]
    assert "CERTIFICATE_VERIFY_FAILED" in t.errors[0]["message"] and t.errors[0]["retryable"] is False
    assert llm.chat_requests() == [] and front.handshakes_ok == 0, "the client hung up during the handshake: nothing reached the server"


def test_turning_verification_off_takes_effect_on_the_very_next_request_without_a_restart(backend: httpx.Client, tls: tuple) -> None:
    llm, front = tls
    assert [e["code"] for e in _turn(backend, "tls-1").errors] == ["chat.llm.tls_cert_untrusted"]
    r = _set_verify(backend, False)
    assert r.json()["ssl_verify"] is False and r.json().get("needs_reboot") in (False, None)
    llm.clear()
    t = _turn(backend, "tls-2")
    assert t.errors == [] and t.text == "ECHO: over tls" and t.ends == ["completed", "snapshot"]
    assert len(llm.chat_requests()) == 1 and front.handshakes_ok >= 1
    assert {k.lower(): v for k, v in llm.chat_requests()[0].headers.items()}["authorization"] == "Bearer e2e-key"


def test_turning_it_back_on_is_just_as_immediate(backend: httpx.Client, tls: tuple) -> None:
    llm, _front = tls
    _set_verify(backend, False)
    assert _turn(backend, "tls-a").text == "ECHO: over tls"
    _set_verify(backend, True)
    llm.clear()
    t = _turn(backend, "tls-b")
    assert t.text == "" and [e["code"] for e in t.errors] == ["chat.llm.tls_cert_untrusted"] and llm.chat_requests() == []


def test_the_flag_can_be_flipped_repeatedly(backend: httpx.Client, tls: tuple) -> None:
    llm, _front = tls
    seen = []
    for value in (False, True, False, True, False):
        _set_verify(backend, value)
        t = _turn(backend, f"tls-flip-{len(seen)}")
        seen.append((value, t.text == "ECHO: over tls", [e["code"] for e in t.errors]))
    assert seen == [(False, True, []), (True, False, ["chat.llm.tls_cert_untrusted"]), (False, True, []), (True, False, ["chat.llm.tls_cert_untrusted"]), (False, True, [])]


def test_an_unrelated_runtime_setting_does_not_change_verification(backend: httpx.Client, tls: tuple) -> None:
    _set_verify(backend, False)
    r = backend.put(f"{SEC}/runtime-config", json={"file_broker_enabled": True})
    assert r.status_code == 200
    assert _turn(backend, "tls-unrelated").text == "ECHO: over tls", "still off after changing another field"


def test_the_stored_value_and_the_effective_behaviour_agree(backend: httpx.Client, tls: tuple) -> None:
    for value in (False, True):
        _set_verify(backend, value)
        assert backend.get(f"{SEC}/runtime-config").json()["ssl_verify"] is value
        ok = _turn(backend, f"tls-agree-{value}").text == "ECHO: over tls"
        assert ok is (value is False)


def test_a_rejected_update_leaves_verification_as_it_was(backend: httpx.Client, tls: tuple) -> None:
    _set_verify(backend, False)
    r = backend.put(f"{SEC}/runtime-config", json={"ssl_verify": "maybe"})
    assert r.status_code == 400
    assert backend.get(f"{SEC}/runtime-config").json()["ssl_verify"] is False
    assert _turn(backend, "tls-rejected").text == "ECHO: over tls"


def test_turning_verification_off_does_not_break_plain_http_endpoints(backend: httpx.Client) -> None:
    _set_verify(backend, False)
    assert backend.get("/api/system/health").status_code == 200 and backend.get("/api/chat/tools").status_code == 200
