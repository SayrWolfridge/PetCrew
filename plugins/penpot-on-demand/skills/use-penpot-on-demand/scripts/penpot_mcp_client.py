from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_URL = "http://localhost:4401/mcp"
PROTOCOL_VERSION = "2025-03-26"


class McpError(RuntimeError):
    pass


def _decode_response(body: bytes, content_type: str, request_id: int) -> dict[str, Any]:
    text = body.decode("utf-8")
    payloads: list[dict[str, Any]] = []
    if "text/event-stream" in content_type:
        for block in text.replace("\r\n", "\n").split("\n\n"):
            data_lines = [line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")]
            if not data_lines:
                continue
            payload = json.loads("\n".join(data_lines))
            if isinstance(payload, dict):
                payloads.append(payload)
    elif text.strip():
        payload = json.loads(text)
        if isinstance(payload, dict):
            payloads.append(payload)

    for payload in payloads:
        if payload.get("id") == request_id:
            return payload
    raise McpError(f"MCP response did not contain JSON-RPC id {request_id}")


class StreamableHttpClient:
    def __init__(self, url: str, timeout: float) -> None:
        self.url = url
        self.timeout = timeout
        self.session_id: str | None = None

    def _post(self, payload: dict[str, Any], *, expect_response: bool = True) -> dict[str, Any] | None:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self.session_id = session_id
                body = response.read()
                if not expect_response:
                    return None
                return _decode_response(body, response.headers.get("Content-Type", ""), int(payload["id"]))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise McpError(f"MCP HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise McpError(f"Penpot MCP is unavailable at {self.url}: {exc.reason}") from exc

    def request(self, request_id: int, method: str, params: dict[str, Any] | None = None) -> Any:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        response = self._post(payload)
        if response is None:
            raise McpError("MCP returned an empty response")
        if "error" in response:
            raise McpError(json.dumps(response["error"], ensure_ascii=False))
        return response.get("result")

    def initialize(self) -> None:
        self.request(
            1,
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "petcrew-penpot-on-demand", "version": "0.1.0"},
            },
        )
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_response=False)


def _arguments(path: str | None) -> dict[str, Any]:
    if path is None:
        return {}
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise McpError("arguments file must contain one JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded local client for Penpot Streamable HTTP MCP")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--timeout", type=float, default=130.0)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list")
    call_parser = subparsers.add_parser("call")
    call_parser.add_argument("tool_name")
    call_parser.add_argument("--arguments-file")
    args = parser.parse_args()

    try:
        client = StreamableHttpClient(args.url, args.timeout)
        client.initialize()
        if args.command == "list":
            result = client.request(2, "tools/list", {})
        else:
            result = client.request(
                2,
                "tools/call",
                {"name": args.tool_name, "arguments": _arguments(args.arguments_file)},
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (McpError, json.JSONDecodeError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
