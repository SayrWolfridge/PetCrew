from __future__ import annotations

import base64
import hmac
import json
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from codex_app_reader import capture_thread_anchor, read_thread_delta
from codex_relay import THREAD_ID_PATTERN


SERVER_HOST = "127.0.0.1"
SERVER_PORT = 4099
MAX_REQUEST_BYTES = 1024 * 1024


class CodexReaderServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        password: str,
        opencode_health_probe: Callable[[], bool],
    ) -> None:
        super().__init__(address, CodexReaderHandler)
        token = base64.b64encode(f"opencode:{password}".encode("utf-8")).decode("ascii")
        self.expected_authorization = f"Basic {token}"
        self.opencode_health_probe = opencode_health_probe


class CodexReaderHandler(BaseHTTPRequestHandler):
    server: CodexReaderServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied, self.server.expected_authorization)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _require_authorization(self) -> bool:
        if self._authorized():
            return True
        self._send_json(401, {"error": "unauthorized"})
        return False

    def _read_body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "0")
        except ValueError as error:
            raise ValueError("invalid Content-Length") from error
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("invalid request size")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise ValueError("invalid JSON request") from error
        if not isinstance(payload, dict):
            raise ValueError("request body must be an object")
        return payload

    @staticmethod
    def _thread_id(payload: dict[str, Any]) -> str:
        value = payload.get("thread_id")
        if not isinstance(value, str) or THREAD_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("valid thread_id is required")
        return value.lower()

    def do_GET(self) -> None:
        if self.path == "/health":
            try:
                opencode_ready = self.server.opencode_health_probe() is True
            except Exception:
                opencode_ready = False
            self._send_json(
                200,
                {
                    "healthy": opencode_ready,
                    "relay_ready": True,
                    "opencode_ready": opencode_ready,
                    "service": "codex-return-reader",
                    "protocol_version": 1,
                },
            )
            return
        if not self._require_authorization():
            return
        if self.path != "/health":
            self._send_json(404, {"error": "not found"})
            return

    def do_POST(self) -> None:
        if not self._require_authorization():
            return
        try:
            payload = self._read_body()
            thread_id = self._thread_id(payload)
            if self.path == "/v1/codex/anchor":
                result = capture_thread_anchor(thread_id)
            elif self.path == "/v1/codex/delta":
                anchor = payload.get("anchor")
                if not isinstance(anchor, dict):
                    raise ValueError("valid anchor is required")
                result = read_thread_delta(thread_id, anchor)
            else:
                self._send_json(404, {"error": "not found"})
                return
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        except RuntimeError as error:
            self._send_json(409, {"error": str(error)})
            return
        except Exception as error:
            self._send_json(500, {"error": type(error).__name__})
            return
        self._send_json(200, {"result": result})


def create_codex_reader_server(
    password: str,
    opencode_health_probe: Callable[[], bool],
    *,
    host: str = SERVER_HOST,
    port: int = SERVER_PORT,
) -> CodexReaderServer:
    if not isinstance(password, str) or not password:
        raise ValueError("reader password is required")
    return CodexReaderServer((host, port), password, opencode_health_probe)
