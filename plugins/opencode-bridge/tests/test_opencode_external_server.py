from __future__ import annotations

import base64
import importlib.util
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "opencode_external_server_test",
    SCRIPTS / "opencode_external_server.py",
)
assert SPEC and SPEC.loader
server_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = server_module
SPEC.loader.exec_module(server_module)


class OpenCodeExternalServerTests(unittest.TestCase):
    def test_health_probe_requires_authenticated_contract(self) -> None:
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = json.dumps(
            {"healthy": True, "version": "1.18.32"}
        ).encode("utf-8")
        response.__enter__.return_value = response
        password = "test-password"

        with mock.patch.object(
            server_module.urllib.request,
            "urlopen",
            return_value=response,
        ) as urlopen:
            self.assertTrue(server_module._opencode_server_healthy(password))

        request = urlopen.call_args.args[0]
        expected = base64.b64encode(f"opencode:{password}".encode("utf-8")).decode(
            "ascii"
        )
        self.assertEqual(request.get_header("Authorization"), f"Basic {expected}")

    def test_health_probe_fails_closed(self) -> None:
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = b'{"healthy":true}'
        response.__enter__.return_value = response
        with mock.patch.object(
            server_module.urllib.request,
            "urlopen",
            return_value=response,
        ):
            self.assertFalse(server_module._opencode_server_healthy("test-password"))

        with mock.patch.object(
            server_module.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("unavailable"),
        ):
            self.assertFalse(server_module._opencode_server_healthy("test-password"))


if __name__ == "__main__":
    unittest.main()
