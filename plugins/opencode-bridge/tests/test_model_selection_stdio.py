from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).parents[1]
THREAD_ID = "019f9a58-1f22-7f63-bd1c-7c480dd3ea1d"


class ModelSelectionStdioTests(unittest.TestCase):
    def test_mcp_model_selection_reaches_http_without_changing_followups(self) -> None:
        calls: list[tuple[str, str, dict, dict]] = []
        password = "isolated-model-selection-fixture"
        expected_auth = "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()
        next_session = 0

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def reply(self, status: int, payload=None) -> None:
                encoded = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def authorized(self) -> bool:
                if self.headers.get("Authorization") != expected_auth:
                    self.reply(401, {"error": "fixture authentication required"})
                    return False
                return True

            def do_GET(self) -> None:
                if self.authorized():
                    if self.path == "/global/health":
                        self.reply(200, {"healthy": True, "version": "fixture"})
                    else:
                        self.reply(404, {"error": "unexpected fixture request"})

            def do_POST(self) -> None:
                nonlocal next_session
                if not self.authorized():
                    return
                parsed = urlsplit(self.path)
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                calls.append(("POST", parsed.path, body, parse_qs(parsed.query)))
                if parsed.path == "/session":
                    next_session += 1
                    self.reply(200, {"id": f"ses_fixture_{next_session}"})
                elif parsed.path.endswith("/prompt_async"):
                    if body.get("model", {}).get("modelID") == "rejected":
                        self.reply(400, {"error": "fixture provider rejected model"})
                    else:
                        self.reply(204)
                else:
                    self.reply(404, {"error": "unexpected fixture request"})

            def do_DELETE(self) -> None:
                if self.authorized():
                    parsed = urlsplit(self.path)
                    calls.append(("DELETE", parsed.path, {}, parse_qs(parsed.query)))
                    self.reply(204)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                environment = os.environ.copy()
                environment.update({
                    "PYTHONUTF8": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "LOCALAPPDATA": folder,
                    "OPENCODE_BRIDGE_EXTERNAL_URL": f"http://127.0.0.1:{server.server_port}",
                    "OPENCODE_BRIDGE_PASSWORD": password,
                    "NO_PROXY": "127.0.0.1,localhost",
                })
                arguments = {"workspace": folder, "prompt": "Fixture only", "codex_thread_id": THREAD_ID}

                def call(request_id, name, args):
                    return {
                        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                        "params": {"name": name, "arguments": args},
                    }

                messages = [
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    call(3, "opencode_start_task", {**arguments, "model": "invalid"}),
                    call(4, "opencode_start_task", arguments),
                    call(5, "opencode_start_task", {**arguments, "model": "fixture-provider/family/model"}),
                    call(6, "opencode_continue", {**arguments, "session_id": "ses_fixture_2", "model": "other/model"}),
                    call(7, "opencode_continue", {**arguments, "session_id": "ses_fixture_2"}),
                    call(8, "opencode_start_task", {**arguments, "model": "fixture-provider/rejected"}),
                ]
                completed = subprocess.run(
                    [sys.executable, "-B", str(ROOT / "scripts" / "opencode_bridge.py"), "mcp"],
                    cwd=ROOT, env=environment, input="".join(json.dumps(item) + "\n" for item in messages),
                    capture_output=True, text=True, encoding="utf-8", timeout=15,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                responses = {item["id"]: item for item in map(json.loads, completed.stdout.splitlines())}
                tools = {tool["name"]: tool for tool in responses[2]["result"]["tools"]}
                self.assertIn("model", tools["opencode_start_task"]["inputSchema"]["properties"])
                for request_id in (3, 6, 8):
                    self.assertTrue(responses[request_id]["result"]["isError"])
                for request_id, model in ((4, "opencode/mimo-v2.6-flash-free"), (5, "fixture-provider/family/model")):
                    receipt = responses[request_id]["result"]["structuredContent"]
                    self.assertEqual(receipt["model"], model)
                    self.assertEqual(receipt["state"], "submitted")
                    self.assertTrue(receipt["wake_on_completion"])
                    self.assertTrue(receipt["persistent_parent_binding"])
                self.assertFalse(responses[7]["result"]["isError"])
                bindings = list((Path(folder) / "opencode-bridge" / "relay" / "bindings").glob("*.json"))
                self.assertEqual(len(bindings), 2)
                for binding_path in bindings:
                    binding = json.loads(binding_path.read_text(encoding="utf-8"))
                    self.assertEqual(binding["codex_thread_id"], THREAD_ID)
                    self.assertEqual(binding["workspace"], str(Path(folder).resolve()))
                expected_workspace = str(Path(folder).resolve())
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)

        self.assertEqual([(method, path) for method, path, _, _ in calls], [
            ("POST", "/session"), ("POST", "/session/ses_fixture_1/prompt_async"),
            ("POST", "/session"), ("POST", "/session/ses_fixture_2/prompt_async"),
            ("POST", "/session/ses_fixture_2/prompt_async"),
            ("POST", "/session"), ("POST", "/session/ses_fixture_3/prompt_async"),
            ("DELETE", "/session/ses_fixture_3"),
        ])
        self.assertEqual(calls[1][2]["model"], {"providerID": "opencode", "modelID": "mimo-v2.6-flash-free"})
        self.assertEqual(calls[3][2]["model"], {"providerID": "fixture-provider", "modelID": "family/model"})
        self.assertNotIn("model", calls[4][2])
        self.assertEqual(calls[6][2]["model"], {"providerID": "fixture-provider", "modelID": "rejected"})
        for _, _, _, query in calls:
            self.assertEqual(query["directory"], [expected_workspace])


if __name__ == "__main__":
    unittest.main()
