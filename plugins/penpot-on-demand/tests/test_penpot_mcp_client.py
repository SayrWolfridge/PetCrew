from __future__ import annotations

import importlib.util
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


CLIENT_PATH = (
    Path(__file__).parents[1]
    / "skills"
    / "use-penpot-on-demand"
    / "scripts"
    / "penpot_mcp_client.py"
)
SPEC = importlib.util.spec_from_file_location("penpot_mcp_client", CLIENT_PATH)
assert SPEC is not None and SPEC.loader is not None
client_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client_module)


class _Handler(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.calls.append({"payload": payload, "session": self.headers.get("Mcp-Session-Id")})

        if payload.get("method") == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return

        request_id = payload["id"]
        if payload["method"] == "initialize":
            result = {"protocolVersion": client_module.PROTOCOL_VERSION, "capabilities": {}}
        elif payload["method"] == "tools/list":
            result = {"tools": [{"name": "penpot_ping", "inputSchema": {"type": "object"}}]}
        else:
            result = {"content": [{"type": "text", "text": "ok"}]}

        body = (
            "event: message\n"
            + "data: "
            + json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})
            + "\n\n"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Mcp-Session-Id", "test-session")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PenpotMcpClientTests(unittest.TestCase):
    def setUp(self) -> None:
        _Handler.calls = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_initialize_then_list_reuses_session(self) -> None:
        url = f"http://127.0.0.1:{self.server.server_port}/mcp"
        client = client_module.StreamableHttpClient(url, 2)
        client.initialize()
        result = client.request(2, "tools/list", {})

        self.assertEqual(result["tools"][0]["name"], "penpot_ping")
        self.assertEqual([row["payload"]["method"] for row in _Handler.calls], [
            "initialize",
            "notifications/initialized",
            "tools/list",
        ])
        self.assertIsNone(_Handler.calls[0]["session"])
        self.assertEqual(_Handler.calls[1]["session"], "test-session")
        self.assertEqual(_Handler.calls[2]["session"], "test-session")

    def test_default_url_uses_localhost_for_ipv4_or_ipv6_loopback(self) -> None:
        self.assertEqual(client_module.DEFAULT_URL, "http://localhost:4401/mcp")

    def test_arguments_file_requires_object(self) -> None:
        path = Path(__file__).with_name("arguments-array.json")
        path.write_text("[]", encoding="utf-8")
        try:
            with self.assertRaises(client_module.McpError):
                client_module._arguments(str(path))
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
