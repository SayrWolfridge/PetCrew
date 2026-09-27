from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

from codex_reader_server import create_codex_reader_server
from opencode_relay import run_relay_forever


SERVER_HOST = "127.0.0.1"
SERVER_PORT = "4098"


def _read_shared_password() -> str:
    password = os.environ.get("OPENCODE_BRIDGE_PASSWORD")
    if password:
        return password
    if os.name == "nt":
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, "OPENCODE_BRIDGE_PASSWORD")
        if isinstance(value, str) and value:
            return value
    raise RuntimeError("OPENCODE_BRIDGE_PASSWORD is not configured")


def _find_opencode() -> str:
    command = shutil.which("opencode")
    if command:
        command_path = Path(command)
        if os.name == "nt" and command_path.suffix.lower() in {".cmd", ".bat"}:
            native = command_path.parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
            if native.is_file():
                return str(native)
        return command

    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            native = Path(appdata) / "npm" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
            if native.is_file():
                return str(native)

    raise RuntimeError("opencode CLI is not available in PATH or the standard user npm installation")


def _server_log_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home()
    return root / "opencode-bridge" / "server.log"


def _bridge_inline_config(existing: str | None) -> str:
    """Preserve inline overrides but disable Playwright MCP for headless Bridge."""
    try:
        config = json.loads(existing) if existing else {}
    except json.JSONDecodeError as error:
        raise RuntimeError("OPENCODE_CONFIG_CONTENT is not valid JSON") from error
    if not isinstance(config, dict):
        raise RuntimeError("OPENCODE_CONFIG_CONTENT must contain a JSON object")
    mcp = config.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        raise RuntimeError("OPENCODE_CONFIG_CONTENT.mcp must be a JSON object")
    playwright = mcp.setdefault("playwright", {})
    if not isinstance(playwright, dict):
        raise RuntimeError("OPENCODE_CONFIG_CONTENT.mcp.playwright must be a JSON object")
    playwright["enabled"] = False
    return json.dumps(config, ensure_ascii=True, separators=(",", ":"))


def _opencode_server_healthy(password: str, timeout: float = 0.8) -> bool:
    raw = f"opencode:{password}".encode("utf-8")
    authorization = "Basic " + base64.b64encode(raw).decode("ascii")
    request = urllib.request.Request(
        f"http://{SERVER_HOST}:{SERVER_PORT}/global/health",
        headers={"Authorization": authorization},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("healthy") is True
        and isinstance(payload.get("version"), str)
        and bool(payload["version"])
    )


def main() -> int:
    password = _read_shared_password()
    environment = os.environ.copy()
    environment["OPENCODE_SERVER_PASSWORD"] = password
    environment["OPENCODE_CONFIG_CONTENT"] = _bridge_inline_config(
        environment.get("OPENCODE_CONFIG_CONTENT")
    )
    stop_event = threading.Event()
    reader_server = create_codex_reader_server(
        password,
        lambda: _opencode_server_healthy(password),
    )
    reader_thread = threading.Thread(
        target=reader_server.serve_forever,
        kwargs={"poll_interval": 0.5},
        name="codex-return-reader",
        daemon=True,
    )
    reader_thread.start()
    relay_thread = threading.Thread(
        target=run_relay_forever,
        args=(stop_event,),
        name="petcrew-relay",
        daemon=True,
    )
    relay_thread.start()
    try:
        log_path = _server_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8", buffering=1) as log_stream:
            process = subprocess.run(
                [
                    _find_opencode(),
                    "serve",
                    "--hostname",
                    SERVER_HOST,
                    "--port",
                    SERVER_PORT,
                ],
                cwd=str(Path.home()),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        return process.returncode
    finally:
        stop_event.set()
        relay_thread.join(timeout=2)
        reader_server.shutdown()
        reader_server.server_close()
        reader_thread.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
