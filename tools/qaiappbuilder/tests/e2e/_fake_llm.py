# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""A scriptable OpenAI-compatible stand-in LLM for chat tests (CHAT-02/05/06/10).

It speaks just enough of ``POST /v1/chat/completions`` (streaming and not) and ``GET /v1/models`` for the product's chat
transport. The reply is controlled by the LAST user message, so one running server serves every test without any
reconfiguration:

* ``[echo] text``      -> replies ``ECHO: text``
* ``[slow] n``         -> streams ``n`` words ("w0 w1 ...") one every ``SLOW_DELAY`` seconds (for interrupt tests)
* ``[error 500]``      -> HTTP 500 with a JSON error body (any status 400..599)
* ``[stall]``          -> sends the headers, then nothing (the client's read timeout / interrupt)
* ``[cut]``            -> streams two words and then closes the socket without ``[DONE]``
* ``[tool name {json}]``-> answers with one ``tool_calls`` delta for ``name`` instead of text; once the tool RESULT comes back it
  finishes with plain text so the round trip ends
* ``[loop name {json}]``-> like ``[tool ...]`` but keeps calling the tool after every result (never converges)
* anything else        -> ``Hello from the stand-in.``

Every request is recorded (``requests``) so tests can assert what the product actually sent: the ``Authorization`` header,
the model id, the message list and the sampling parameters.

Nothing here reaches the network: it binds 127.0.0.1 on an ephemeral port.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
from dataclasses import dataclass, field

SLOW_DELAY = 0.25
MODEL_IDS = ("e2e-model", "e2e-model-2")


@dataclass
class Recorded:
    method: str
    path: str
    headers: dict[str, str]
    body: dict | None


@dataclass
class StandInLLM:
    base_url: str
    requests: list[Recorded] = field(default_factory=list)
    words_sent: int = 0           # words written by [slow] replies, counted at the SERVER
    open_streams: int = 0         # currently open [slow]/[stall] streams
    _server: http.server.ThreadingHTTPServer | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def chat_requests(self) -> list[Recorded]:
        """The chat TURN's own calls (streaming). The product also asks the model for a conversation title in the background
        (``stream: false``, no tools); that call is excluded here and available via ``title_requests``."""
        return [r for r in self.requests if r.path.endswith("/chat/completions") and (r.body or {}).get("stream")]

    def title_requests(self) -> list[Recorded]:
        return [r for r in self.requests if r.path.endswith("/chat/completions") and not (r.body or {}).get("stream")]

    def clear(self) -> None:
        with self._lock:
            self.requests.clear()
            self.words_sent = 0

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


def _last_user_text(body: dict) -> str:
    for m in reversed(body.get("messages") or []):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                return "".join(p.get("text", "") for p in c if isinstance(p, dict))
            return str(c or "")
    return ""


def start() -> StandInLLM:
    holder: dict[str, StandInLLM] = {}

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a: object) -> None:
            pass

        # -- helpers -----------------------------------------------------
        def _json(self, status: int, obj: dict) -> None:
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _sse_start(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

        def _sse(self, obj: dict | str) -> None:
            payload = obj if isinstance(obj, str) else json.dumps(obj)
            self.wfile.write(f"data: {payload}\n\n".encode())
            self.wfile.flush()

        @staticmethod
        def _chunk(delta: dict, finish: str | None = None) -> dict:
            return {"id": "chatcmpl-e2e", "object": "chat.completion.chunk", "created": 1, "model": "e2e-model",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        # -- routes ------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            s = holder["s"]
            s.requests.append(Recorded("GET", self.path, dict(self.headers), None))
            if self.path.rstrip("/").endswith("/models"):
                self._json(200, {"object": "list", "data": [{"id": m, "object": "model", "created": 1, "owned_by": "e2e"} for m in MODEL_IDS]})
            else:
                self._json(404, {"error": {"message": "not found"}})

        def do_POST(self) -> None:  # noqa: N802
            s = holder["s"]
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = None
            s.requests.append(Recorded("POST", self.path, dict(self.headers), body))
            if not self.path.endswith("/chat/completions") or body is None:
                self._json(404, {"error": {"message": "not found"}})
                return
            text = _last_user_text(body)
            stream = bool(body.get("stream"))

            msgs = body.get("messages") or []
            looping = text.startswith("[loop ")   # keep calling the tool even after its result came back (no-progress guard test)
            if msgs and msgs[-1].get("role") == "tool" and not looping:
                # The product sent a tool result back: finish the turn in plain text so a tool round trip ends.
                text = "[echo] TOOL-RESULT: " + str(msgs[-1].get("content"))[:300]

            if text.startswith("[error"):
                code = int("".join(ch for ch in text.split("]")[0] if ch.isdigit()) or 500)
                self._json(code, {"error": {"message": f"stand-in error {code}", "type": "e2e_error", "code": str(code)}})
                return

            if text.startswith("[tool ") or looping:
                rest = text[len("[tool " if text.startswith("[tool ") else "[loop "):].rstrip("]")
                name, _, args = rest.partition(" ")
                call = {"index": 0, "id": "call_e2e_1", "type": "function", "function": {"name": name, "arguments": args or "{}"}}
                if stream:
                    self._sse_start()
                    self._sse(self._chunk({"role": "assistant", "tool_calls": [call]}))
                    self._sse(self._chunk({}, "tool_calls"))
                    self._sse("[DONE]")
                else:
                    self._json(200, {"id": "chatcmpl-e2e", "object": "chat.completion", "created": 1, "model": "e2e-model",
                                     "choices": [{"index": 0, "finish_reason": "tool_calls",
                                                  "message": {"role": "assistant", "content": None, "tool_calls": [{**call, "index": None}]}}],
                                     "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})
                return

            if text.startswith("[stall]"):
                self._sse_start()
                s.open_streams += 1
                try:
                    for _ in range(600):
                        time.sleep(0.1)
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                except Exception:  # noqa: BLE001 - the client went away
                    pass
                finally:
                    s.open_streams -= 1
                return

            if text.startswith("[slow]"):
                count = int("".join(ch for ch in text[6:] if ch.isdigit()) or 20)
                words = [f"w{i} " for i in range(count)]
            elif text.startswith("[cut]"):
                words = ["one ", "two "]
            elif text.startswith("[echo]"):
                words = [f"ECHO: {text[6:].strip()}"]
            else:
                words = ["Hello ", "from ", "the ", "stand-in."]
            full = "".join(words)

            if not stream:
                self._json(200, {"id": "chatcmpl-e2e", "object": "chat.completion", "created": 1, "model": "e2e-model",
                                 "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": full}}],
                                 "usage": {"prompt_tokens": 7, "completion_tokens": len(words), "total_tokens": 7 + len(words)}})
                return

            self._sse_start()
            s.open_streams += 1
            try:
                self._sse(self._chunk({"role": "assistant", "content": ""}))
                for w in words:
                    self._sse(self._chunk({"content": w}))
                    with s._lock:
                        s.words_sent += 1
                    if text.startswith("[slow]"):
                        time.sleep(SLOW_DELAY)
                if text.startswith("[cut]"):
                    self.wfile.flush()
                    self.close_connection = True
                    return                      # no finish chunk, no [DONE]
                last = self._chunk({}, "stop")
                last["usage"] = {"prompt_tokens": 7, "completion_tokens": len(words), "total_tokens": 7 + len(words)}
                self._sse(last)
                self._sse("[DONE]")
            except Exception:  # noqa: BLE001 - the client disconnected mid-stream
                pass
            finally:
                s.open_streams -= 1
                self.close_connection = True

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    s = StandInLLM(base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1")
    s._server = srv
    holder["s"] = s
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return s
