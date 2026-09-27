from __future__ import annotations

import base64
import importlib.util
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "codex_reader_server_test",
    SCRIPTS / "codex_reader_server.py",
)
assert SPEC and SPEC.loader
server_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = server_module
SPEC.loader.exec_module(server_module)


THREAD_ID = "019fd3ed-cb16-7362-af22-cf341aa706d5"


class CodexReaderServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.password = "test-password"
        self.server = server_module.create_codex_reader_server(
            self.password,
            lambda: True,
            port=0,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _authorization(self) -> str:
        encoded = base64.b64encode(
            f"opencode:{self.password}".encode("utf-8")
        ).decode("ascii")
        return f"Basic {encoded}"

    def test_health_is_generic_unauthenticated_and_reports_both_services(self) -> None:
        with urllib.request.urlopen(self.base_url + "/health", timeout=2) as response:
            self.assertEqual(response.status, 200)
            result = json.loads(response.read().decode("utf-8"))
        self.assertEqual(
            result,
            {
                "healthy": True,
                "relay_ready": True,
                "opencode_ready": True,
                "service": "codex-return-reader",
                "protocol_version": 1,
            },
        )

    def test_anchor_endpoint_requires_auth_and_returns_reader_result(self) -> None:
        payload = json.dumps({"thread_id": THREAD_ID}).encode("utf-8")
        unauthorized = urllib.request.Request(
            self.base_url + "/v1/codex/anchor",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(unauthorized, timeout=2)
        self.assertEqual(denied.exception.code, 401)

        anchor = {"turn_id": "turn", "item_hashes": {}}
        authorized = urllib.request.Request(
            self.base_url + "/v1/codex/anchor",
            data=payload,
            headers={
                "Authorization": self._authorization(),
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with mock.patch.object(
            server_module,
            "capture_thread_anchor",
            return_value=anchor,
        ) as capture, urllib.request.urlopen(authorized, timeout=2) as response:
            result = json.loads(response.read().decode("utf-8"))
        self.assertEqual(result["result"], anchor)
        capture.assert_called_once_with(THREAD_ID)


if __name__ == "__main__":
    unittest.main()
