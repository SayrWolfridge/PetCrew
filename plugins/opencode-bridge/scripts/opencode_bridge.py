from __future__ import annotations

import atexit
import base64
from collections import deque
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, TextIO

from codex_relay import (
    binding_status as codex_binding_status,
    binding_status_report as codex_binding_status_report,
    confirm_binding as confirm_codex_binding,
    detach_binding as detach_codex_binding,
    get_return_parent as codex_get_return_parent,
    register_binding as register_codex_binding,
    return_receipt as codex_return_receipt,
    rollback_binding as rollback_codex_binding,
    transfer_return_parent as codex_transfer_return_parent,
)
from opencode_relay import (
    THREAD_ID_PATTERN,
    detach_binding,
    register_binding,
    rollback_binding,
)


SERVER_HOST = "127.0.0.1"
SERVER_PORT = int(os.environ.get("OPENCODE_BRIDGE_PORT", "4097"))
EXTERNAL_SERVER_URL = os.environ.get("OPENCODE_BRIDGE_EXTERNAL_URL", "").strip().rstrip("/")
SERVER_URL = EXTERNAL_SERVER_URL or f"http://{SERVER_HOST}:{SERVER_PORT}"
EXTERNAL_SERVER_MODE = bool(EXTERNAL_SERVER_URL)
CODEX_READER_URL = os.environ.get(
    "CODEX_READER_URL", "http://127.0.0.1:4099"
).strip().rstrip("/")
SERVER_START_TIMEOUT = 20.0
MAX_WAIT_SECONDS = 55
MAX_SESSION_RESULTS = 100
CODEX_READER_HTTP_TIMEOUT_SECONDS = 75.0
MAX_SESSION_MESSAGES = 100
PETCREW_PROTOCOL_VERSION = "1.0"
PETCREW_TERMINAL_PHASES = {"completed", "failed", "cancelled"}
PETCREW_SECRET_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
SERVER_STDERR_MAX_LINES = 40
SERVER_STDERR_MAX_CHARS = 8000
SERVER_PROCESS: subprocess.Popen[bytes] | None = None
SERVER_PASSWORD: str | None = None
SERVER_STDERR_LINES: deque[str] = deque(maxlen=SERVER_STDERR_MAX_LINES)
SERVER_STDERR_THREAD: threading.Thread | None = None
COMPLETION_ID_PATTERN = re.compile(r"^completion:[0-9a-f]{64}$")
DEFAULT_START_MODEL = {
    "providerID": "opencode",
    "modelID": "mimo-v2.6-flash-free",
}
START_MODEL_PATTERN = re.compile(r"^[^/\s]+/[^/\s]+(?:/[^/\s]+)*$")


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
    *,
    read_only: bool = False,
) -> dict[str, Any]:
    tool = {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": required or [],
        },
    }
    if read_only:
        tool["annotations"] = {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    return tool


TOOLS = [
    _tool(
        "opencode_health",
        "Start the private localhost OpenCode server if needed and report its health.",
        {},
        read_only=True,
    ),
    _tool(
        "codex_bind_return",
        (
            "Persist an event-driven return route from one delegated Codex task to the "
            "exact source Codex task before sending the follow-up. Does not send a message."
        ),
        {
            "workspace": {
                "type": "string",
                "description": "Existing absolute source-task project directory.",
            },
            "target_thread_id": {
                "type": "string",
                "pattern": (
                    "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                    "[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
                ),
            },
            "source_thread_id": {
                "type": "string",
                "pattern": (
                    "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                    "[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
                ),
            },
            "target_cursor": {
                "type": "string",
                "minLength": 1,
                "maxLength": 2048,
                "description": "Cursor from the mandatory fresh wait snapshot of the target task.",
            },
            "transfer_parent": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Explicitly move this target from another source Codex task. "
                    "Use only with user approval."
                ),
            },
        },
        ["workspace", "target_thread_id", "source_thread_id", "target_cursor"],
    ),
    _tool(
        "codex_confirm_return",
        "Confirm a prepared Codex return route after the target follow-up was accepted.",
        {
            "workspace": {"type": "string"},
            "target_thread_id": {
                "type": "string",
                "pattern": (
                    "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                    "[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
                ),
            },
            "binding_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
            "rollback_token": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
        ["workspace", "target_thread_id", "binding_id", "rollback_token"],
    ),
    _tool(
        "codex_rollback_return",
        "Rollback the exact prepared Codex return route when the target follow-up failed to send.",
        {
            "workspace": {"type": "string"},
            "target_thread_id": {
                "type": "string",
                "pattern": (
                    "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                    "[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
                ),
            },
            "binding_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
            "rollback_token": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
        ["workspace", "target_thread_id", "binding_id", "rollback_token"],
    ),
    _tool(
        "codex_detach_return",
        "Detach a delegated Codex target from its exact source task.",
        {
            "workspace": {"type": "string"},
            "target_thread_id": {
                "type": "string",
                "pattern": (
                    "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                    "[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
                ),
            },
            "source_thread_id": {
                "type": "string",
                "pattern": (
                    "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                    "[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
                ),
            },
        },
        ["workspace", "target_thread_id", "source_thread_id"],
    ),
    _tool(
        "codex_return_status",
        "Read-only: inspect the persisted return route and distinguish unbound, in-flight, delivered, invalid-state, and stale-runtime conditions.",
        {
            "workspace": {"type": "string"},
            "target_thread_id": {
                "type": "string",
                "pattern": (
                    "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                    "[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
                ),
            },
        },
        ["workspace", "target_thread_id"],
        read_only=True,
    ),
    _tool(
        "codex_read_return",
        (
            "Read-only: verify one exact PetCrew terminal receipt and return only the "
            "matching delegated Codex task delta captured after its persisted baseline."
        ),
        {
            "workspace": {
                "type": "string",
                "description": "Existing absolute source-task project directory.",
            },
            "target_thread_id": {
                "type": "string",
                "pattern": (
                    "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                    "[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
                ),
            },
            "source_thread_id": {
                "type": "string",
                "pattern": (
                    "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                    "[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
                ),
            },
            "completion_id": {
                "type": "string",
                "pattern": "^completion:[0-9a-f]{64}$",
            },
        },
        ["workspace", "target_thread_id", "source_thread_id", "completion_id"],
        read_only=True,
    ),
    _tool(
        "codex_get_return_parent",
        "Read-only: inspect the current source parent of a delegated Codex target.",
        {
            "target_thread_id": {
                "type": "string",
                "pattern": (
                    "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                    "[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
                ),
            },
        },
        ["target_thread_id"],
        read_only=True,
    ),
    _tool(
        "codex_transfer_return_parent",
        (
            "Transfer the source parent of a delegated Codex target to a new source task. "
            "Requires explicit user approval and transfer_parent=true."
        ),
        {
            "target_thread_id": {
                "type": "string",
                "pattern": (
                    "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                    "[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
                ),
            },
            "expected_binding_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
            "new_source_thread_id": {
                "type": "string",
                "pattern": (
                    "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                    "[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
                ),
            },
            "new_workspace": {
                "type": "string",
                "description": "Existing absolute directory for the new source task workspace.",
            },
            "transfer_parent": {
                "type": "boolean",
                "description": "Must be true to confirm explicit parent transfer.",
            },
        },
        [
            "target_thread_id",
            "expected_binding_id",
            "new_source_thread_id",
            "new_workspace",
            "transfer_parent",
        ],
    ),
    _tool(
        "opencode_list_sessions",
        (
            "Read-only: list existing OpenCode sessions in a workspace. Optionally filter "
            "by session id or title. Does not access the OpenCode database directly."
        ),
        {
            "workspace": {
                "type": "string",
                "description": "Existing absolute project directory.",
            },
            "query": {
                "type": "string",
                "description": "Optional case-insensitive substring of session id or title.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_SESSION_RESULTS,
                "default": 20,
            },
        },
        ["workspace"],
        read_only=True,
    ),
    _tool(
        "opencode_read_session",
        (
            "Read-only: return metadata and user/assistant text from an existing OpenCode "
            "session in a workspace. Omits reasoning and tool payloads."
        ),
        {
            "workspace": {
                "type": "string",
                "description": "Existing absolute project directory.",
            },
            "session_id": {"type": "string", "pattern": "^ses"},
            "message_limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_SESSION_MESSAGES,
                "default": 50,
            },
            "completion_id": {
                "type": "string",
                "pattern": "^completion:[0-9a-f]{64}$",
                "description": "Optional PetCrew terminal receipt to select one exact assistant response.",
            },
            "phase": {
                "type": "string",
                "enum": ["completed", "failed", "cancelled"],
                "default": "completed",
            },
        },
        ["workspace", "session_id"],
        read_only=True,
    ),
    _tool(
        "opencode_start_task",
        (
            "Create an OpenCode session and send a task asynchronously. Defaults to the safe "
            "plan agent. Use agent=build only when the user authorized implementation."
        ),
        {
            "workspace": {
                "type": "string",
                "description": "Existing absolute project directory.",
            },
            "prompt": {"type": "string", "minLength": 1},
            "title": {"type": "string", "maxLength": 160},
            "agent": {"type": "string", "enum": ["plan", "build"], "default": "plan"},
            "model": {
                "type": "string",
                "pattern": START_MODEL_PATTERN.pattern,
                "description": (
                    "Optional provider/model for this new task only. Omit to use "
                    "opencode/mimo-v2.6-flash-free. No provider/model substitution."
                ),
            },
            "wake_on_completion": {
                "type": "boolean",
                "default": True,
                "description": "Resume this Codex task once when PetCrew reports completion.",
            },
            "codex_thread_id": {
                "type": "string",
                "pattern": (
                    "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                    "[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
                ),
                "description": (
                    "Exact current Codex task id. Read CODEX_THREAD_ID from the current "
                    "task environment before calling this tool."
                ),
            },
        },
        ["workspace", "prompt", "codex_thread_id"],
    ),
    _tool(
        "opencode_continue",
        "Send a follow-up message asynchronously to an existing OpenCode session.",
        {
            "workspace": {"type": "string"},
            "session_id": {"type": "string", "pattern": "^ses"},
            "prompt": {"type": "string", "minLength": 1},
            "agent": {"type": "string", "enum": ["plan", "build"]},
            "wake_on_completion": {
                "type": "boolean",
                "default": True,
                "description": "Resume this Codex task once when PetCrew reports completion.",
            },
            "codex_thread_id": {
                "type": "string",
                "pattern": (
                    "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                    "[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
                ),
                "description": (
                    "Exact current Codex task id. Read CODEX_THREAD_ID from the current "
                    "task environment before calling this tool."
                ),
            },
            "transfer_parent": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Explicitly transfer this OpenCode session from another parent Codex "
                    "task. Use only with user approval."
                ),
            },
        },
        ["workspace", "session_id", "prompt", "codex_thread_id"],
    ),
    _tool(
        "opencode_detach",
        "Detach an OpenCode session from its parent Codex task so later completions do not wake it.",
        {
            "workspace": {"type": "string"},
            "session_id": {"type": "string", "pattern": "^ses"},
            "codex_thread_id": {
                "type": "string",
                "pattern": (
                    "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                    "[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
                ),
            },
        },
        ["workspace", "session_id", "codex_thread_id"],
    ),
    _tool(
        "opencode_wait",
        (
            "Wait event-driven (no polling) for PetCrew to report that an OpenCode session "
            "completed, failed, or was cancelled, then return the latest assistant text. "
            "Also recovers retained completions from the PetCrew inbox."
        ),
        {
            "workspace": {"type": "string"},
            "session_id": {"type": "string", "pattern": "^ses"},
            "timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_WAIT_SECONDS,
                "default": 30,
            },
        },
        ["workspace", "session_id"],
        read_only=True,
    ),
    _tool(
        "opencode_answer_question",
        (
            "Answer a pending OpenCode question. Answers must follow question order; each "
            "inner array contains selected labels or one free-form answer."
        ),
        {
            "workspace": {"type": "string"},
            "request_id": {"type": "string", "pattern": "^que"},
            "answers": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "string"}},
            },
        },
        ["workspace", "request_id", "answers"],
    ),
    _tool(
        "opencode_reply_permission",
        (
            "Reply once or reject a pending OpenCode permission. Never use 'once' without "
            "the user's explicit approval for the exact action. Permanent approval is not exposed."
        ),
        {
            "workspace": {"type": "string"},
            "request_id": {"type": "string", "pattern": "^per"},
            "reply": {"type": "string", "enum": ["once", "reject"]},
            "message": {"type": "string", "maxLength": 500},
        },
        ["workspace", "request_id", "reply"],
    ),
    _tool(
        "opencode_abort",
        "Abort a running OpenCode session.",
        {
            "workspace": {"type": "string"},
            "session_id": {"type": "string", "pattern": "^ses"},
        },
        ["workspace", "session_id"],
    ),
]


def _validate_workspace(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("workspace must be a non-empty absolute path")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("workspace must be an absolute path")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("workspace must be an existing directory")
    return str(resolved)


def _validate_submission_workspace(workspace: str) -> None:
    marker = Path(workspace) / ".git"
    try:
        if marker.is_dir() and next(marker.iterdir(), None) is None:
            raise RuntimeError(
                "workspace contains an empty .git directory; "
                "OpenCode task was not submitted"
            )
        if marker.is_file() and marker.stat().st_size == 0:
            raise RuntimeError(
                "workspace contains an empty .git file; "
                "OpenCode task was not submitted"
            )
    except OSError as error:
        raise RuntimeError(
            "workspace .git metadata could not be inspected; "
            "OpenCode task was not submitted"
        ) from error


def _start_model(arguments: dict[str, Any]) -> dict[str, str]:
    if "model" not in arguments:
        return DEFAULT_START_MODEL.copy()
    raw_model = arguments["model"]
    if not isinstance(raw_model, str) or START_MODEL_PATTERN.fullmatch(raw_model) is None:
        raise ValueError("model must be a provider/model string without empty segments or whitespace")
    provider_id, model_id = raw_model.split("/", 1)
    return {"providerID": provider_id, "modelID": model_id}


def _completion_route_request(arguments: dict[str, Any]) -> tuple[bool, str | None]:
    wake_on_completion = arguments.get("wake_on_completion", True)
    if not isinstance(wake_on_completion, bool):
        raise ValueError("wake_on_completion must be a boolean")
    raw_thread_id = arguments.get("codex_thread_id")
    if not wake_on_completion and raw_thread_id is None:
        return False, None
    if not isinstance(raw_thread_id, str):
        if wake_on_completion:
            raise ValueError(
                "codex_thread_id is required before submitting a wake-enabled OpenCode task"
            )
        raise ValueError("valid codex_thread_id is required")
    thread_id = raw_thread_id.strip()
    if THREAD_ID_PATTERN.fullmatch(thread_id) is None:
        raise ValueError("valid codex_thread_id is required")
    return wake_on_completion, thread_id.lower()


def _query(workspace: str) -> str:
    return urllib.parse.urlencode({"directory": workspace})


def _auth_header() -> str:
    if SERVER_PASSWORD is None:
        raise RuntimeError("OpenCode server password is not initialized")
    raw = f"opencode:{SERVER_PASSWORD}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _petcrew_app_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA is not configured")
    return Path(local_app_data) / "app.petcrew.overlay"


def _petcrew_discover(timeout: float = 1.0) -> tuple[str, str]:
    app_dir = _petcrew_app_dir().resolve()
    descriptor_path = app_dir / "hub-runtime.json"
    try:
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeError("PetCrew Monitor is not running or its runtime descriptor is invalid") from error
    if not isinstance(descriptor, dict) or descriptor.get("protocol_version") != PETCREW_PROTOCOL_VERSION:
        raise RuntimeError("PetCrew completion protocol 1.0 is not available")
    endpoint = descriptor.get("endpoint")
    if not isinstance(endpoint, str):
        raise RuntimeError("PetCrew runtime endpoint is invalid")
    parsed = urllib.parse.urlsplit(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname != SERVER_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.port is None
    ):
        raise RuntimeError("PetCrew runtime endpoint must be a plain 127.0.0.1 loopback URL")
    secret_raw = descriptor.get("secret_file")
    if not isinstance(secret_raw, str):
        raise RuntimeError("PetCrew runtime secret path is invalid")
    secret_path = Path(secret_raw).resolve()
    if secret_path.parent != app_dir:
        raise RuntimeError("PetCrew runtime secret must stay inside its app-data directory")
    try:
        secret = secret_path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError("PetCrew runtime secret is unavailable") from error
    if PETCREW_SECRET_PATTERN.fullmatch(secret) is None:
        raise RuntimeError("PetCrew runtime secret is invalid")
    with _petcrew_request(endpoint.rstrip("/"), secret, "/health", timeout=timeout) as response:
        try:
            health = json.loads(response.read().decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise RuntimeError("PetCrew returned an invalid health response") from error
    if not isinstance(health, dict) or health.get("protocol_version") != PETCREW_PROTOCOL_VERSION:
        raise RuntimeError("PetCrew completion protocol 1.0 is not healthy")
    return endpoint.rstrip("/"), secret


def _petcrew_request(
    endpoint: str,
    secret: str,
    path: str,
    *,
    timeout: float,
    accept: str = "application/json",
) -> Any:
    request = urllib.request.Request(
        endpoint + path,
        headers={"Authorization": f"Bearer {secret}", "Accept": accept},
    )
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise RuntimeError(
                "PetCrew Monitor is running, but its completion inbox is not installed "
                "(GET /v1/completions returned 404)"
            ) from error
        if error.code == 401:
            raise RuntimeError("PetCrew authentication changed; rediscovery is required") from error
        raise RuntimeError(f"PetCrew HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"PetCrew Monitor is unavailable: {error.reason}") from error


def _petcrew_session_id(raw_session_id: str) -> str:
    digest = hashlib.sha256(raw_session_id.strip().encode("utf-8")).hexdigest()
    return f"session:{digest}"


def _petcrew_inbox(after: int, *, timeout: float = 2.0) -> dict[str, Any]:
    endpoint, secret = _petcrew_discover(timeout=min(timeout, 1.0))
    path = "/v1/completions?" + urllib.parse.urlencode({"after": after})
    with _petcrew_request(endpoint, secret, path, timeout=timeout) as response:
        try:
            payload = json.loads(response.read().decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise RuntimeError("PetCrew returned an invalid completion inbox") from error
    if not isinstance(payload, dict):
        raise RuntimeError("PetCrew returned an invalid completion inbox")
    return payload


def _valid_completion(record: Any) -> bool:
    return (
        isinstance(record, dict)
        and isinstance(record.get("cursor"), int)
        and isinstance(record.get("completion_id"), str)
        and COMPLETION_ID_PATTERN.fullmatch(record["completion_id"]) is not None
        and isinstance(record.get("session_id"), str)
        and record.get("provider") == "opencode"
        and record.get("phase") in PETCREW_TERMINAL_PHASES
    )


def _matching_inbox_completion(session_id: str) -> tuple[dict[str, Any] | None, int]:
    payload = _petcrew_inbox(0)
    latest_cursor = payload.get("latest_cursor", 0)
    if not isinstance(latest_cursor, int):
        raise RuntimeError("PetCrew returned an invalid latest cursor")
    if payload.get("truncated") is True:
        return None, latest_cursor
    expected = _petcrew_session_id(session_id)
    records = payload.get("completions")
    if not isinstance(records, list):
        raise RuntimeError("PetCrew returned an invalid completion list")
    matches = [
        record
        for record in records
        if _valid_completion(record) and record.get("session_id") == expected
    ]
    matches.sort(key=lambda record: record["cursor"])
    return (matches[-1] if matches else None), latest_cursor


def _petcrew_sse_completion(
    session_id: str,
    after: int,
    timeout: int,
) -> dict[str, Any] | None:
    endpoint, secret = _petcrew_discover()
    path = "/v1/completions/stream?" + urllib.parse.urlencode({"after": after})
    expected = _petcrew_session_id(session_id)
    deadline = time.monotonic() + timeout
    try:
        with _petcrew_request(
            endpoint,
            secret,
            path,
            timeout=timeout + 1,
            accept="text/event-stream",
        ) as response:
            event_name = ""
            data_lines: list[str] = []
            while time.monotonic() < deadline:
                raw_line = response.readline()
                if not raw_line:
                    return None
                line = raw_line.decode("utf-8", errors="strict").rstrip("\r\n")
                if line == "":
                    if event_name == "completion" and data_lines:
                        try:
                            record = json.loads("\n".join(data_lines))
                        except ValueError as error:
                            raise RuntimeError("PetCrew returned invalid completion SSE data") from error
                        if _valid_completion(record) and record.get("session_id") == expected:
                            return record
                    event_name = ""
                    data_lines = []
                elif line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
    except TimeoutError:
        return None
    return None


def _read_shared_password() -> str | None:
    password = os.environ.get("OPENCODE_BRIDGE_PASSWORD")
    if password:
        return password
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, "OPENCODE_BRIDGE_PASSWORD")
        return value if isinstance(value, str) and value else None
    except (FileNotFoundError, OSError):
        return None


def _request(
    method: str,
    path: str,
    *,
    workspace: str | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> Any:
    ensure_server()
    if workspace:
        separator = "&" if "?" in path else "?"
        path = f"{path}{separator}{_query(workspace)}"
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": _auth_header(), "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(
        SERVER_URL + path,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            if not payload:
                return None
            content_type = response.headers.get("Content-Type", "")
            if "json" in content_type:
                return json.loads(payload.decode("utf-8"))
            return payload.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenCode HTTP {error.code}: {detail[:1000]}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"OpenCode server unavailable: {error.reason}") from error


def _codex_reader_request(path: str, body: dict[str, Any]) -> dict[str, Any]:
    ensure_server()
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        CODEX_READER_URL + path,
        data=data,
        headers={
            "Authorization": _auth_header(),
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=CODEX_READER_HTTP_TIMEOUT_SECONDS,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Codex return reader HTTP {error.code}: {detail[:1000]}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Codex return reader is unavailable: {error.reason}") from error
    except (UnicodeDecodeError, ValueError) as error:
        raise RuntimeError("Codex return reader returned invalid JSON") from error
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError("Codex return reader returned an invalid result")
    return result


def capture_thread_anchor(thread_id: str) -> dict[str, Any]:
    return _codex_reader_request(
        "/v1/codex/anchor",
        {"thread_id": thread_id},
    )


def read_thread_delta(thread_id: str, anchor: dict[str, Any]) -> dict[str, Any]:
    return _codex_reader_request(
        "/v1/codex/delta",
        {"thread_id": thread_id, "anchor": anchor},
    )


def _health(password: str | None, timeout: float = 1.0) -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    if password is not None:
        raw = f"opencode:{password}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
    request = urllib.request.Request(SERVER_URL + "/global/health", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None


EXTERNAL_HEALTH_TIMEOUTS = (1.5, 3.0, 5.0)
EXTERNAL_HEALTH_BACKOFF_SECONDS = (0.2, 0.4)


def _external_health(
    timeouts: tuple[float, ...] = EXTERNAL_HEALTH_TIMEOUTS,
) -> dict[str, Any] | None:
    bounded_timeouts = tuple(timeout for timeout in timeouts if timeout > 0)
    if not bounded_timeouts:
        bounded_timeouts = EXTERNAL_HEALTH_TIMEOUTS
    for attempt, timeout in enumerate(bounded_timeouts):
        current = _health(SERVER_PASSWORD, timeout=timeout)
        if current and current.get("healthy") is True:
            return current
        if attempt + 1 < len(bounded_timeouts):
            backoff_index = min(attempt, len(EXTERNAL_HEALTH_BACKOFF_SECONDS) - 1)
            time.sleep(EXTERNAL_HEALTH_BACKOFF_SECONDS[backoff_index])
    return None


def _find_opencode() -> str:
    command = shutil.which("opencode")
    if not command:
        raise RuntimeError("opencode CLI is not available in PATH")
    command_path = Path(command)
    if os.name == "nt" and command_path.suffix.lower() in {".cmd", ".bat"}:
        native = command_path.parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
        if native.is_file():
            return str(native)
    return command


def _capture_server_stderr(stream: Any) -> None:
    try:
        for raw_line in iter(stream.readline, b""):
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            if line:
                SERVER_STDERR_LINES.append(line[-2000:])
    finally:
        stream.close()


def _server_stderr_tail() -> str:
    tail = "\n".join(SERVER_STDERR_LINES)[-SERVER_STDERR_MAX_CHARS:]
    if SERVER_PASSWORD:
        tail = tail.replace(SERVER_PASSWORD, "[REDACTED]")
    return tail


def _server_failure(message: str) -> RuntimeError:
    tail = _server_stderr_tail()
    if not tail:
        return RuntimeError(message)
    detail = f"{message}\nCaptured stderr:\n{tail}"
    if "EEXIST" in tail and str(Path.home() / ".config" / "opencode") in tail:
        detail += (
            "\nThe existing OpenCode config directory was not modified. "
            "This error can mean the child process cannot access that directory."
        )
    return RuntimeError(detail)


def ensure_server() -> dict[str, Any]:
    global SERVER_PASSWORD, SERVER_PROCESS, SERVER_STDERR_THREAD
    if EXTERNAL_SERVER_MODE:
        SERVER_PASSWORD = _read_shared_password()
        if not SERVER_PASSWORD:
            raise RuntimeError(
                "OPENCODE_BRIDGE_PASSWORD is not configured for the external OpenCode server"
            )
        current = _external_health()
        if current is not None:
            return current
        raise RuntimeError(
            f"External OpenCode server is not reachable at {SERVER_URL}. "
            "Check the Windows task 'OpenCode Bridge Server'."
        )
    if SERVER_PASSWORD is not None:
        current = _health(SERVER_PASSWORD)
        if current and current.get("healthy") is True:
            return current
    if _health(None) is not None:
        raise RuntimeError(
            f"Port {SERVER_PORT} is occupied by an unauthenticated OpenCode server; "
            "stop it or set OPENCODE_BRIDGE_PORT to another port"
        )
    SERVER_PASSWORD = secrets.token_urlsafe(32)
    environment = os.environ.copy()
    environment["OPENCODE_SERVER_PASSWORD"] = SERVER_PASSWORD
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    SERVER_STDERR_LINES.clear()
    SERVER_PROCESS = subprocess.Popen(
        [
            _find_opencode(),
            "serve",
            "--hostname",
            SERVER_HOST,
            "--port",
            str(SERVER_PORT),
        ],
        cwd=str(Path.home()),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=flags,
    )
    assert SERVER_PROCESS.stderr is not None
    SERVER_STDERR_THREAD = threading.Thread(
        target=_capture_server_stderr,
        args=(SERVER_PROCESS.stderr,),
        name="opencode-bridge-stderr",
        daemon=True,
    )
    SERVER_STDERR_THREAD.start()
    deadline = time.monotonic() + SERVER_START_TIMEOUT
    while time.monotonic() < deadline:
        if SERVER_PROCESS.poll() is not None:
            SERVER_STDERR_THREAD.join(timeout=0.5)
            raise _server_failure(
                f"OpenCode server exited with code {SERVER_PROCESS.returncode}"
            )
        current = _health(SERVER_PASSWORD)
        if current and current.get("healthy") is True:
            return current
        time.sleep(0.2)
    failure = _server_failure("OpenCode server did not become healthy in time")
    stop_server()
    raise failure


def stop_server() -> None:
    global SERVER_PROCESS, SERVER_STDERR_THREAD
    process = SERVER_PROCESS
    SERVER_PROCESS = None
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    if SERVER_STDERR_THREAD is not None:
        SERVER_STDERR_THREAD.join(timeout=0.5)
        SERVER_STDERR_THREAD = None


atexit.register(stop_server)


def _text_parts(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        info = message.get("info")
        if not isinstance(info, dict) or info.get("role") != "assistant":
            continue
        texts = []
        for part in message.get("parts", []):
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
        if texts:
            return "\n\n".join(texts)
    return ""


def _session_view(session: Any) -> dict[str, Any]:
    if not isinstance(session, dict):
        return {}
    return {
        key: session[key]
        for key in (
            "id",
            "title",
            "slug",
            "directory",
            "parentID",
            "projectID",
            "version",
            "time",
            "summary",
        )
        if key in session
    }


def _message_view(message: Any) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return None
    info = message.get("info")
    if not isinstance(info, dict):
        return None
    role = info.get("role")
    if role not in {"user", "assistant"}:
        return None
    texts = []
    parts = message.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
    if not texts:
        return None
    return {
        "id": info.get("id"),
        "role": role,
        "time": info.get("time"),
        "agent": info.get("agent"),
        "model": info.get("model")
        or {
            key: info[key]
            for key in ("providerID", "modelID")
            if key in info
        },
        "text": "\n\n".join(texts),
    }


def _terminal_completion_id(
    raw_session_id: str,
    assistant_message_id: str,
    phase: str,
) -> str:
    receipt = f"{raw_session_id.strip()}\0{assistant_message_id.strip()}\0{phase}"
    event_id = "terminal:" + hashlib.sha256(receipt.encode("utf-8")).hexdigest()
    return "completion:" + hashlib.sha256(event_id.encode("utf-8")).hexdigest()


def _matching_terminal_message(
    raw_messages: Any,
    session_id: str,
    completion_id: str,
    phase: str,
) -> dict[str, Any] | None:
    if not isinstance(raw_messages, list) or phase != "completed":
        return None
    for message in raw_messages:
        info = message.get("info") if isinstance(message, dict) else None
        if (
            isinstance(info, dict)
            and info.get("role") == "assistant"
            and isinstance(info.get("id"), str)
            and isinstance(info.get("time"), dict)
            and isinstance(info["time"].get("completed"), (int, float))
            and _terminal_completion_id(session_id, info["id"], phase) == completion_id
        ):
            return message
    return None


def _workspace_sessions(workspace: str) -> list[dict[str, Any]]:
    sessions = _request("GET", "/session", workspace=workspace) or []
    if not isinstance(sessions, list):
        raise RuntimeError("OpenCode returned an invalid session list")
    return [session for session in sessions if isinstance(session, dict)]


def _pending(workspace: str, session_id: str) -> tuple[list[Any], list[Any]]:
    questions = _request("GET", "/question", workspace=workspace) or []
    permissions = _request("GET", "/permission", workspace=workspace) or []
    questions = [
        item
        for item in questions
        if isinstance(item, dict) and item.get("sessionID") == session_id
    ]
    permissions = [
        item
        for item in permissions
        if isinstance(item, dict) and item.get("sessionID") == session_id
    ]
    return questions, permissions


def _result_text(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, indent=2),
            }
        ],
        "structuredContent": payload,
        "isError": False,
    }


def call_tool(name: str, arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    if name == "opencode_health":
        payload = {"server": SERVER_URL, **ensure_server()}
        try:
            inbox = _petcrew_inbox(0)
            payload["petcrew_completion"] = {
                "available": True,
                "latest_cursor": inbox.get("latest_cursor"),
            }
        except RuntimeError as error:
            payload["petcrew_completion"] = {"available": False, "error": str(error)}
        return _result_text(payload)

    if name == "codex_get_return_parent":
        target_thread_id = arguments.get("target_thread_id")
        result = codex_get_return_parent(target_thread_id)
        return _result_text(result)

    if name == "codex_transfer_return_parent":
        target_thread_id = arguments.get("target_thread_id")
        expected_binding_id = arguments.get("expected_binding_id")
        new_source_thread_id = arguments.get("new_source_thread_id")
        new_workspace_raw = arguments.get("new_workspace")
        transfer_parent = arguments.get("transfer_parent", False)
        if not isinstance(transfer_parent, bool) or not transfer_parent:
            raise ValueError("transfer_parent=true is required")
        new_workspace = _validate_workspace(new_workspace_raw)
        result = codex_transfer_return_parent(
            target_thread_id,
            expected_binding_id,
            new_source_thread_id,
            new_workspace,
            transfer_parent=transfer_parent,
        )
        return _result_text(result)

    workspace = _validate_workspace(arguments.get("workspace"))
    if name == "codex_bind_return":
        transfer_parent = arguments.get("transfer_parent", False)
        if not isinstance(transfer_parent, bool):
            raise ValueError("transfer_parent must be a boolean")
        inbox = _petcrew_inbox(0)
        latest_cursor = inbox.get("latest_cursor")
        if not isinstance(latest_cursor, int) or isinstance(latest_cursor, bool):
            raise RuntimeError("PetCrew returned an invalid latest cursor")
        target_anchor = capture_thread_anchor(arguments.get("target_thread_id"))
        binding, changed, rollback_token = register_codex_binding(
            arguments.get("target_thread_id"),
            arguments.get("source_thread_id"),
            workspace,
            arguments.get("target_cursor"),
            latest_cursor,
            {
                "turn_id": target_anchor.get("turn_id"),
                "item_hashes": target_anchor.get("item_hashes", {}),
            },
            transfer_parent=transfer_parent,
        )
        return _result_text(
            {
                "state": "bound",
                "target_thread_id": binding["target_codex_thread_id"],
                "source_thread_id": binding["source_codex_thread_id"],
                "target_cursor": binding["target_cursor"],
                "binding_id": binding["binding_id"],
                "binding_changed": changed,
                "rollback_token": rollback_token,
                "baseline_completion_cursor": binding["after_cursor"],
                "baseline_turn_id": binding["baseline_anchor"].get("turn_id"),
                "baseline_turn_status": target_anchor.get("turn_status"),
                "baseline_visible_items": target_anchor.get("visible_items", []),
                "baseline_omitted_items": target_anchor.get("omitted_items", 0),
                "wake_on_completion": True,
                "return_state": binding["return_state"],
                "return_armed": binding["return_state"] == "armed",
                "completion_claimable": binding["return_state"] in {"prepared", "armed"},
                "one_shot_return": True,
                "persistent_parent_binding": True,
            }
        )

    if name in {"codex_confirm_return", "codex_rollback_return"}:
        target_thread_id = arguments.get("target_thread_id")
        binding_id = arguments.get("binding_id")
        rollback_token = arguments.get("rollback_token")
        if not isinstance(binding_id, str) or re.fullmatch(r"[0-9a-f]{32}", binding_id) is None:
            raise ValueError("valid binding_id is required")
        if (
            not isinstance(rollback_token, str)
            or re.fullmatch(r"[0-9a-f]{64}", rollback_token) is None
        ):
            raise ValueError("valid rollback_token is required")
        if name == "codex_rollback_return":
            rolled_back = rollback_codex_binding(
                target_thread_id,
                binding_id,
                rollback_token,
            )
            return _result_text(
                {
                    "target_thread_id": target_thread_id,
                    "rolled_back": rolled_back,
                }
            )
        confirmed_now = confirm_codex_binding(
            target_thread_id,
            binding_id,
            rollback_token,
        )
        status = codex_binding_status(target_thread_id)
        active_and_confirmed = (
            status is not None
            and status.get("binding_id") == binding_id
            and not status.get("pending_confirmation")
        )
        if not confirmed_now and not active_and_confirmed:
            raise RuntimeError("Codex return binding confirmation failed")
        return _result_text(
            {
                "target_thread_id": target_thread_id,
                "binding_id": binding_id,
                "confirmed": True,
                "already_confirmed": not confirmed_now,
                "return_state": status["return_state"],
                "return_armed": status["return_state"] == "armed",
                "completion_claimable": status["return_state"] in {"prepared", "armed"},
                "one_shot_return": True,
                "persistent_parent_binding": True,
            }
        )

    if name == "codex_detach_return":
        target_thread_id = arguments.get("target_thread_id")
        source_thread_id = arguments.get("source_thread_id")
        detached = detach_codex_binding(target_thread_id, source_thread_id)
        return _result_text(
            {
                "target_thread_id": target_thread_id,
                "source_thread_id": source_thread_id,
                "detached": detached,
            }
        )

    if name == "codex_return_status":
        target_thread_id = arguments.get("target_thread_id")
        report = codex_binding_status_report(target_thread_id)
        status = report.get("binding")
        visible = None
        if status is not None:
            visible = {
                "binding_id": status["binding_id"],
                "target_thread_id": status["target_codex_thread_id"],
                "source_thread_id": status["source_codex_thread_id"],
                "workspace": status["workspace"],
                "target_cursor": status["target_cursor"],
                "bound_at": status["bound_at"],
                "baseline_completion_cursor": status["after_cursor"],
                "last_attempted_cursor": status["last_attempted_cursor"],
                "attempted_completion_count": len(status["attempted_completion_ids"]),
                "delivered_completion_count": len(status["delivered_completion_ids"]),
                "in_flight_completion_count": len(
                    report.get("in_flight_completion_ids", [])
                ),
                "in_flight_completion_ids": report.get(
                    "in_flight_completion_ids", []
                ),
                "baseline_turn_id": status["baseline_anchor"].get("turn_id"),
                "pending_confirmation": status["pending_confirmation"],
                "return_state": status["return_state"],
                "return_armed": status["return_state"] == "armed",
                "completion_claimable": status["return_state"] in {"prepared", "armed"},
                "one_shot_return": True,
                "persistent_parent_binding": True,
            }
        state = report.get("state", "state_unavailable")
        bound = (
            True
            if state == "bound" and visible is not None
            else (False if state == "unbound" else None)
        )
        return _result_text(
            {
                "target_thread_id": target_thread_id,
                "state": state,
                "bound": bound,
                "binding": visible,
                "diagnostic": report.get("diagnostic"),
                "runtime": report.get("runtime"),
            }
        )

    if name == "codex_read_return":
        target_thread_id = arguments.get("target_thread_id")
        source_thread_id = arguments.get("source_thread_id")
        completion_id = arguments.get("completion_id")
        receipt = codex_return_receipt(
            target_thread_id,
            source_thread_id,
            workspace,
            completion_id,
        )
        delta = read_thread_delta(target_thread_id, receipt["anchor"])
        return _result_text(
            {
                "target_thread_id": target_thread_id,
                "source_thread_id": source_thread_id,
                "completion_id": completion_id,
                "phase": receipt["phase"],
                "receipt_state": receipt["receipt_state"],
                "matching_terminal_receipt": True,
                "delta": delta,
            }
        )

    if name == "opencode_list_sessions":
        query = arguments.get("query", "")
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        limit = arguments.get("limit", 20)
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("limit must be an integer")
        limit = max(1, min(MAX_SESSION_RESULTS, limit))
        sessions = _workspace_sessions(workspace)
        needle = query.strip().casefold()
        if needle:
            sessions = [
                session
                for session in sessions
                if needle
                in f"{session.get('id', '')} {session.get('title', '')}".casefold()
            ]
        sessions.sort(
            key=lambda session: (
                session.get("time", {}).get("updated", 0)
                if isinstance(session.get("time"), dict)
                else 0
            ),
            reverse=True,
        )
        visible = [_session_view(session) for session in sessions[:limit]]
        return _result_text(
            {
                "workspace": workspace,
                "query": query.strip(),
                "count": len(visible),
                "sessions": visible,
            }
        )

    if name == "opencode_read_session":
        session_id = arguments.get("session_id")
        if not isinstance(session_id, str) or not session_id.startswith("ses"):
            raise ValueError("valid session_id is required")
        message_limit = arguments.get("message_limit", 50)
        if not isinstance(message_limit, int) or isinstance(message_limit, bool):
            raise ValueError("message_limit must be an integer")
        message_limit = max(1, min(MAX_SESSION_MESSAGES, message_limit))
        completion_id = arguments.get("completion_id")
        phase = arguments.get("phase", "completed")
        if completion_id is not None and (
            not isinstance(completion_id, str)
            or COMPLETION_ID_PATTERN.fullmatch(completion_id) is None
        ):
            raise ValueError("valid completion_id is required")
        if phase not in PETCREW_TERMINAL_PHASES:
            raise ValueError("phase must be completed, failed, or cancelled")
        sessions = _workspace_sessions(workspace)
        session = next(
            (item for item in sessions if item.get("id") == session_id),
            None,
        )
        if session is None:
            raise ValueError("session_id was not found in this workspace")
        raw_messages = _request(
            "GET",
            f"/session/{session_id}/message?limit={message_limit}",
            workspace=workspace,
        )
        if not isinstance(raw_messages, list):
            raise RuntimeError("OpenCode returned an invalid message list")
        if completion_id is not None:
            matched = _matching_terminal_message(
                raw_messages,
                session_id,
                completion_id,
                phase,
            )
            if matched is None:
                raise ValueError("completion_id was not found in this OpenCode session")
            raw_messages = [matched]
        messages = []
        for raw_message in raw_messages:
            visible_message = _message_view(raw_message)
            if visible_message is not None:
                messages.append(visible_message)
        return _result_text(
            {
                "workspace": workspace,
                "session": _session_view(session),
                "messages": messages,
                "latest_assistant_text": _text_parts(raw_messages),
                "completion_id": completion_id,
            }
        )

    if name == "opencode_start_task":
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt is required")
        agent = arguments.get("agent", "plan")
        if agent not in {"plan", "build"}:
            raise ValueError("agent must be plan or build")
        model = _start_model(arguments)
        wake_on_completion, codex_thread_id = _completion_route_request(arguments)
        _validate_submission_workspace(workspace)
        create_body: dict[str, Any] = {
            "title": arguments.get("title") or prompt.strip()[:120],
            "agent": agent,
        }
        session = _request("POST", "/session", workspace=workspace, body=create_body)
        session_id = session.get("id") if isinstance(session, dict) else None
        if not isinstance(session_id, str):
            raise RuntimeError("OpenCode did not return a session id")
        try:
            registration = (
                register_binding(session_id, workspace, codex_thread_id)
                if wake_on_completion and codex_thread_id is not None
                else None
            )
        except Exception:
            _request("DELETE", f"/session/{session_id}", workspace=workspace)
            raise
        if wake_on_completion and registration is None:
            _request("DELETE", f"/session/{session_id}", workspace=workspace)
            raise RuntimeError("parent binding was not created; OpenCode task was not submitted")
        try:
            _request(
                "POST",
                f"/session/{session_id}/prompt_async",
                workspace=workspace,
                body={
                    "agent": agent,
                    "model": model,
                    "parts": [{"type": "text", "text": prompt}],
                },
            )
        except Exception:
            if registration is not None:
                binding, changed, previous = registration
                if changed:
                    rollback_binding(session_id, binding["binding_id"], previous)
            _request("DELETE", f"/session/{session_id}", workspace=workspace)
            raise
        return _result_text(
            {
                "session_id": session_id,
                "workspace": workspace,
                "agent": agent,
                "model": f"{model['providerID']}/{model['modelID']}",
                "state": "submitted",
                "wake_on_completion": registration is not None,
                "persistent_parent_binding": registration is not None,
            }
        )

    session_id = arguments.get("session_id")
    if name in {"opencode_continue", "opencode_wait", "opencode_abort", "opencode_detach"}:
        if not isinstance(session_id, str) or not session_id.startswith("ses"):
            raise ValueError("valid session_id is required")

    if name == "opencode_continue":
        if "model" in arguments:
            raise ValueError(
                "model selection is supported only for new tasks; "
                "opencode_continue preserves the existing session model"
            )
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt is required")
        wake_on_completion, codex_thread_id = _completion_route_request(arguments)
        body: dict[str, Any] = {"parts": [{"type": "text", "text": prompt}]}
        agent = arguments.get("agent")
        if agent is not None:
            if agent not in {"plan", "build"}:
                raise ValueError("agent must be plan or build")
            body["agent"] = agent
        transfer_parent = arguments.get("transfer_parent", False)
        if not isinstance(transfer_parent, bool):
            raise ValueError("transfer_parent must be a boolean")
        _validate_submission_workspace(workspace)
        registration = (
            register_binding(
                session_id,
                workspace,
                codex_thread_id,
                transfer_parent=transfer_parent,
            )
            if wake_on_completion and codex_thread_id is not None
            else None
        )
        if wake_on_completion and registration is None:
            raise RuntimeError("parent binding was not created; OpenCode task was not submitted")
        try:
            _request(
                "POST",
                f"/session/{session_id}/prompt_async",
                workspace=workspace,
                body=body,
            )
        except Exception:
            if registration is not None:
                binding, changed, previous = registration
                if changed:
                    rollback_binding(session_id, binding["binding_id"], previous)
            raise
        return _result_text(
            {
                "session_id": session_id,
                "state": "submitted",
                "wake_on_completion": registration is not None,
                "persistent_parent_binding": registration is not None,
            }
        )

    if name == "opencode_detach":
        raw_thread_id = arguments.get("codex_thread_id")
        if not isinstance(raw_thread_id, str) or THREAD_ID_PATTERN.fullmatch(raw_thread_id) is None:
            raise ValueError("valid codex_thread_id is required")
        detached = detach_binding(session_id, raw_thread_id)
        return _result_text(
            {
                "session_id": session_id,
                "workspace": workspace,
                "detached": detached,
            }
        )

    if name == "opencode_wait":
        timeout_seconds = arguments.get("timeout_seconds", 30)
        if not isinstance(timeout_seconds, int):
            raise ValueError("timeout_seconds must be an integer")
        timeout_seconds = max(1, min(MAX_WAIT_SECONDS, timeout_seconds))
        questions, permissions = _pending(workspace, session_id)
        completion, latest_cursor = _matching_inbox_completion(session_id)
        source = "petcrew_inbox" if completion is not None else None
        if completion is None and not questions and not permissions:
            completion = _petcrew_sse_completion(
                session_id,
                latest_cursor,
                timeout_seconds,
            )
            if completion is not None:
                source = "petcrew_sse"
        questions, permissions = _pending(workspace, session_id)
        statuses = _request("GET", "/session/status", workspace=workspace) or {}
        state: Any = statuses.get(session_id, {"type": "idle"})
        messages = _request(
            "GET",
            f"/session/{session_id}/message?limit=20",
            workspace=workspace,
        )
        if completion is not None and completion.get("phase") == "completed":
            matched = _matching_terminal_message(
                messages,
                session_id,
                completion["completion_id"],
                completion["phase"],
            )
            if matched is None:
                raise RuntimeError(
                    "PetCrew completion receipt was not found in the OpenCode session"
                )
            messages = [matched]
        return _result_text(
            {
                "session_id": session_id,
                "status": state,
                "completion": completion,
                "completion_source": source,
                "questions": questions,
                "permissions": permissions,
                "latest_text": _text_parts(messages),
            }
        )

    if name == "opencode_answer_question":
        request_id = arguments.get("request_id")
        answers = arguments.get("answers")
        if not isinstance(request_id, str) or not request_id.startswith("que"):
            raise ValueError("valid request_id is required")
        if not isinstance(answers, list) or not all(
            isinstance(answer, list)
            and all(isinstance(value, str) for value in answer)
            for answer in answers
        ):
            raise ValueError("answers must be an array of string arrays")
        result = _request(
            "POST",
            f"/question/{request_id}/reply",
            workspace=workspace,
            body={"answers": answers},
        )
        return _result_text({"request_id": request_id, "accepted": bool(result)})

    if name == "opencode_reply_permission":
        request_id = arguments.get("request_id")
        reply = arguments.get("reply")
        if not isinstance(request_id, str) or not request_id.startswith("per"):
            raise ValueError("valid request_id is required")
        if reply not in {"once", "reject"}:
            raise ValueError("reply must be once or reject")
        body: dict[str, Any] = {"reply": reply}
        message = arguments.get("message")
        if isinstance(message, str) and message:
            body["message"] = message
        result = _request(
            "POST",
            f"/permission/{request_id}/reply",
            workspace=workspace,
            body=body,
        )
        return _result_text({"request_id": request_id, "reply": reply, "accepted": bool(result)})

    if name == "opencode_abort":
        result = _request(
            "POST",
            f"/session/{session_id}/abort",
            workspace=workspace,
            body={},
        )
        return _result_text({"session_id": session_id, "aborted": bool(result)})

    raise ValueError(f"unknown tool: {name}")


def _rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def handle_rpc(message: Any) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return _rpc_error(None, -32600, "Invalid Request")
    request_id = message.get("id")
    method = message.get("method")
    if not isinstance(method, str):
        return _rpc_error(request_id, -32600, "Invalid Request")
    if request_id is None and method.startswith("notifications/"):
        return None
    if method == "initialize":
        params = message.get("params")
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        protocol = requested if isinstance(requested, str) and requested else "2024-11-05"
        return _rpc_result(
            request_id,
            {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "opencode-bridge", "version": "0.1.1"},
            },
        )
    if method == "ping":
        return _rpc_result(request_id, {})
    if method == "tools/list":
        return _rpc_result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _rpc_error(request_id, -32602, "Invalid tool call")
        try:
            result = call_tool(params["name"], params.get("arguments", {}))
            return _rpc_result(request_id, result)
        except (ValueError, RuntimeError) as error:
            return _rpc_result(
                request_id,
                {
                    "content": [{"type": "text", "text": str(error)}],
                    "structuredContent": {"error": str(error)},
                    "isError": True,
                },
            )
        except Exception as error:
            return _rpc_result(
                request_id,
                {
                    "content": [{"type": "text", "text": f"OpenCode bridge failed: {error}"}],
                    "structuredContent": {"error": type(error).__name__},
                    "isError": True,
                },
            )
    if method == "shutdown":
        stop_server()
        return _rpc_result(request_id, {})
    return _rpc_error(request_id, -32601, "Method not found")


def run_mcp(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = _rpc_error(None, -32700, "Parse error")
        else:
            response = handle_rpc(message)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            stdout.flush()
    return 0


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "mcp":
        return run_mcp()
    print("usage: opencode_bridge.py mcp", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
