"""Best-effort sanitized bridge from Codex hooks/MCP to the local PetCrew hub."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, TextIO
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import uuid


PROTOCOL_VERSION = "1.0"
APP_DATA_FOLDER = "app.petcrew.overlay"
HTTP_TIMEOUT_SECONDS = 0.25
MAX_HOOK_BYTES = 1_000_000
MAX_RUNTIME_BYTES = 8_192
MAX_SECRET_BYTES = 256
MAX_REGISTRY_EVENT_BYTES = 64 * 1024
REGISTRY_FOLDER = "agent-registry"
TOOL_NAME = "petcrew_report_status"

_sequence_lock = threading.Lock()
_last_sequence = 0
_mcp_instance = uuid.uuid4().hex
_mcp_session_id = f"mcp:{_mcp_instance}"
_mcp_root_agent_id = "root"

_SENSITIVE_MARKERS = (
    "bearer ",
    "password=",
    "password:",
    "api_key=",
    "apikey=",
    "access_token=",
    "secret=",
    "sk-",
)


def next_sequence() -> int:
    global _last_sequence
    with _sequence_lock:
        candidate = time.time_ns()
        _last_sequence = max(candidate, _last_sequence + 1)
        return _last_sequence


def occurred_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _contains_sensitive_marker(value: str) -> bool:
    lower = value.lower()
    return any(marker in lower for marker in _SENSITIVE_MARKERS)


def clean_text(value: Any, fallback: str, limit: int) -> str:
    if not isinstance(value, str):
        return fallback
    cleaned = " ".join(value.split())
    if not cleaned or _contains_sensitive_marker(cleaned):
        return fallback
    return cleaned[:limit]


def stable_id(value: Any, prefix: str) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if not cleaned or _contains_sensitive_marker(cleaned):
        return ""
    candidate = f"{prefix}{cleaned}"
    if len(candidate) <= 300:
        return candidate
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
    return f"{prefix}{digest}"


def opaque_id(value: Any, prefix: str) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if not cleaned or _contains_sensitive_marker(cleaned):
        return ""
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
    return f"{prefix}{digest}"


def project_from_hook(data: dict[str, Any]) -> dict[str, Any] | None:
    cwd = data.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip() or len(cwd) > 4_096:
        return None
    if _contains_sensitive_marker(cwd):
        return None
    normalized = os.path.normcase(os.path.normpath(cwd.strip()))
    name = clean_text(os.path.basename(normalized), "Проект", 120)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return {"id": f"project:{digest}", "name": name, "path": None}


def _runtime_data_dir(override: Path | None = None) -> Path | None:
    if override is not None:
        return override
    explicit = os.environ.get("PETCREW_DATA_DIR")
    if explicit:
        return Path(explicit)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    return Path(local_app_data) / APP_DATA_FOLDER


def discover_connection(data_dir: Path | None = None) -> tuple[str, str] | None:
    root = _runtime_data_dir(data_dir)
    if root is None:
        return None
    runtime_path = root / "hub-runtime.json"
    try:
        if runtime_path.stat().st_size > MAX_RUNTIME_BYTES:
            return None
        descriptor = json.loads(runtime_path.read_text(encoding="utf-8"))
        if not isinstance(descriptor, dict) or descriptor.get("protocol_version") != PROTOCOL_VERSION:
            return None
        endpoint = descriptor.get("endpoint")
        secret_file = descriptor.get("secret_file")
        if not isinstance(endpoint, str) or not isinstance(secret_file, str):
            return None
        parsed = urlparse(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            return None
        root_resolved = root.resolve()
        secret_path = Path(secret_file).resolve()
        if not secret_path.is_relative_to(root_resolved):
            return None
        if secret_path.stat().st_size > MAX_SECRET_BYTES:
            return None
        token = secret_path.read_text(encoding="utf-8").strip()
        if len(token) != 64 or any(character not in "0123456789abcdefABCDEF" for character in token):
            return None
        return endpoint.rstrip("/"), token
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def post_event(
    event: dict[str, Any],
    data_dir: Path | None = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> bool:
    connection = discover_connection(data_dir)
    if connection is None:
        return False
    endpoint, token = connection
    try:
        body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(
            f"{endpoint}/v1/events",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=timeout) as response:
            return response.status == 202
    except Exception:
        return False


def persist_registry_event(event: dict[str, Any], data_dir: Path | None = None) -> bool:
    root = _runtime_data_dir(data_dir)
    if root is None:
        return False
    try:
        provider = event.get("provider")
        session_id = event.get("session_id")
        agent_id = event.get("agent_id")
        if not all(isinstance(value, str) and value for value in (provider, session_id, agent_id)):
            return False
        body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_REGISTRY_EVENT_BYTES:
            return False
        registry = root / REGISTRY_FOLDER
        registry.mkdir(parents=True, exist_ok=True)
        key = "\0".join((provider, session_id, agent_id))
        filename = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json"
        target = registry / filename
        temporary = registry / f".{filename}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_bytes(body)
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return True
    except (OSError, TypeError, ValueError):
        return False


def make_event(
    session_id: str,
    agent_id: str,
    event_type: str,
    payload: dict[str, Any],
    parent_agent_id: str | None = None,
) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "event_id": str(uuid.uuid4()),
        "sequence": next_sequence(),
        "occurred_at": occurred_at(),
        "provider": "codex",
        "session_id": session_id,
        "agent_id": agent_id,
        "parent_agent_id": parent_agent_id,
        "event_type": event_type,
        "payload": payload,
    }


def _result(summary: str, outcome: str) -> dict[str, Any]:
    return {
        "summary": summary,
        "outcome": outcome,
        "completed_at": occurred_at(),
        "unread": True,
    }


def _attention(kind: str, summary: str) -> dict[str, Any]:
    return {"kind": kind, "summary": summary, "requested_at": occurred_at()}


def _plan_progress(tool_input: Any) -> dict[str, Any] | None:
    if not isinstance(tool_input, dict):
        return None
    plan = tool_input.get("plan")
    if not isinstance(plan, list) or not plan or len(plan) > 100:
        return None
    statuses = []
    for item in plan:
        if not isinstance(item, dict):
            return None
        status = item.get("status")
        if status not in {"pending", "in_progress", "completed"}:
            return None
        statuses.append(status)
    completed = statuses.count("completed")
    return {
        "kind": "steps",
        "current": completed,
        "total": len(statuses),
        "label": f"План: {completed} из {len(statuses)}",
        "source": "explicit",
    }


def _hook_payload(
    project: dict[str, Any] | None,
    task: dict[str, Any],
    phase: str,
    current_action: str,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task": task,
        "phase": phase,
        "current_action": current_action,
        **extra,
    }
    if project is not None:
        payload["project"] = project
    return payload


def event_from_hook(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    event_name = data.get("hook_event_name")
    if event_name == "SessionStart":
        return None
    session_id = opaque_id(data.get("session_id"), "session:")
    turn_id = opaque_id(data.get("turn_id"), "turn:")
    if not session_id or not turn_id:
        return None
    if event_name in {"Stop", "SubagentStop"} and data.get("stop_hook_active") is True:
        return None

    root_task = {"title": "Задача Codex", "detail": None}
    child_task = {"title": "Помощник Codex", "detail": None}
    project = project_from_hook(data)

    if event_name == "UserPromptSubmit":
        return make_event(
            session_id,
            turn_id,
            "agent.started",
            _hook_payload(project, root_task, "working", "Начал задачу"),
        )
    if event_name == "PreToolUse":
        tool_name = data.get("tool_name")
        normalized_tool = tool_name.strip().lower() if isinstance(tool_name, str) else ""
        if normalized_tool in {"update_plan", "functions.update_plan"}:
            progress = _plan_progress(data.get("tool_input"))
            if progress is not None:
                return make_event(
                    session_id,
                    turn_id,
                    "agent.progress",
                    _hook_payload(
                        project,
                        root_task,
                        "working",
                        "Выполняет план",
                        progress=progress,
                    ),
                )
            return None
        if normalized_tool in {
            "request_user_input",
            "functions.request_user_input",
        }:
            summary = "Ждёт выбора"
            return make_event(
                session_id,
                turn_id,
                "agent.attention_requested",
                _hook_payload(
                    project,
                    root_task,
                    "waiting_input",
                    summary,
                    attention=_attention("input", summary),
                ),
            )
        return None
    if event_name == "PostToolUse":
        tool_name = data.get("tool_name")
        normalized_tool = tool_name.strip().lower() if isinstance(tool_name, str) else ""
        if normalized_tool not in {
            "request_user_input",
            "functions.request_user_input",
        }:
            return None
        return make_event(
            session_id,
            turn_id,
            "agent.activity",
            _hook_payload(project, root_task, "working", "Продолжает работу"),
        )
    if event_name == "PermissionRequest":
        summary = "Ждёт подтверждения"
        return make_event(
            session_id,
            turn_id,
            "agent.attention_requested",
            _hook_payload(
                project,
                root_task,
                "waiting_approval",
                summary,
                attention=_attention("approval", summary),
            ),
        )
    if event_name in {"SubagentStart", "SubagentStop"}:
        child_id = opaque_id(data.get("agent_id", data.get("agentId")), "agent:")
        if not child_id:
            return None
        if event_name == "SubagentStart":
            return make_event(
                session_id,
                child_id,
                "agent.started",
                _hook_payload(project, child_task, "working", "Подключился к задаче"),
                parent_agent_id=turn_id,
            )
        summary = "Закончил свою часть"
        return make_event(
            session_id,
            child_id,
            "agent.completed",
            _hook_payload(
                project,
                child_task,
                "completed",
                summary,
                result=_result(summary, "success"),
            ),
            parent_agent_id=turn_id,
        )
    if event_name == "Stop":
        summary = "Задача завершена"
        return make_event(
            session_id,
            turn_id,
            "agent.completed",
            _hook_payload(
                project,
                root_task,
                "completed",
                summary,
                result=_result(summary, "success"),
            ),
        )
    return None


_REPORT_STATUSES = {
    "started",
    "working",
    "progress",
    "waiting_input",
    "waiting_approval",
    "blocked",
    "completed",
    "failed",
    "cancelled",
}

_REPORT_FIELDS = {
    "status",
    "task_title",
    "action",
    "progress_label",
    "current",
    "total",
    "summary",
    "session_id",
    "agent_id",
    "parent_agent_id",
}


def event_from_report(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    if set(arguments) - _REPORT_FIELDS:
        raise ValueError("unsupported fields")
    status = arguments.get("status")
    if status not in _REPORT_STATUSES:
        raise ValueError("invalid status")
    raw_title = arguments.get("task_title")
    if not isinstance(raw_title, str) or not raw_title.strip():
        raise ValueError("task_title is required")
    task_title = clean_text(raw_title, "Задача Codex", 120)
    action = clean_text(arguments.get("action"), "", 160)
    label = clean_text(arguments.get("progress_label"), action, 160)
    summary = clean_text(arguments.get("summary"), "", 500)

    session_id = stable_id(arguments.get("session_id"), "") or _mcp_session_id
    agent_id = stable_id(arguments.get("agent_id"), "") or _mcp_root_agent_id
    parent_agent_id = stable_id(arguments.get("parent_agent_id"), "") or None
    if parent_agent_id == agent_id:
        raise ValueError("parent_agent_id must differ from agent_id")

    current = arguments.get("current")
    total = arguments.get("total")
    if (current is None) != (total is None):
        raise ValueError("current and total must be supplied together")
    progress: dict[str, Any] | None = None
    if current is not None:
        if type(current) is not int or type(total) is not int:
            raise ValueError("current and total must be integers")
        if total <= 0 or current < 0 or current > total:
            raise ValueError("invalid progress range")
        progress = {
            "kind": "steps",
            "current": current,
            "total": total,
            "label": label or f"Шаг {current} из {total}",
            "source": "explicit",
        }
    elif status == "progress":
        progress = {
            "kind": "indeterminate",
            "current": None,
            "total": None,
            "label": label or "Продолжает работу",
            "source": "unavailable",
        }

    event_type = "agent.activity"
    phase = "working"
    if status == "started":
        event_type = "agent.started"
        action = action or "Начал задачу"
    elif status == "progress":
        event_type = "agent.progress"
        action = action or label or "Продолжает работу"
    elif status == "working":
        action = action or "Продолжает работу"
    elif status in {"waiting_input", "waiting_approval", "blocked"}:
        event_type = "agent.attention_requested"
        phase = status
        default_summary = {
            "waiting_input": "Ждёт ответа",
            "waiting_approval": "Ждёт подтверждения",
            "blocked": "Работа заблокирована",
        }[status]
        summary = summary or action or default_summary
        action = action or summary
    elif status in {"completed", "failed", "cancelled"}:
        event_type = {
            "completed": "agent.completed",
            "failed": "agent.failed",
            "cancelled": "agent.cancelled",
        }[status]
        phase = status
        default_summary = {
            "completed": "Задача завершена",
            "failed": "Задача завершилась с ошибкой",
            "cancelled": "Задача отменена",
        }[status]
        summary = summary or action or default_summary
        action = action or summary

    payload: dict[str, Any] = {
        "task": {"title": task_title, "detail": None},
        "phase": phase,
        "current_action": action,
    }
    if progress is not None:
        payload["progress"] = progress
    if status in {"waiting_input", "waiting_approval", "blocked"}:
        attention_kind = {
            "waiting_input": "input",
            "waiting_approval": "approval",
            "blocked": "blocked",
        }[status]
        payload["attention"] = _attention(attention_kind, summary or action)
    if status in {"completed", "failed", "cancelled"}:
        outcome = {"completed": "success", "failed": "failure", "cancelled": "cancelled"}[status]
        payload["result"] = _result(summary, outcome)

    return make_event(session_id, agent_id, event_type, payload, parent_agent_id)


TOOL_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": "Report a concise, sanitized task or agent status to the local PetCrew overlay.",
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "task_title"],
        "properties": {
            "status": {"type": "string", "enum": sorted(_REPORT_STATUSES)},
            "task_title": {"type": "string", "minLength": 1, "maxLength": 120},
            "action": {"type": "string", "maxLength": 160},
            "progress_label": {"type": "string", "maxLength": 160},
            "current": {"type": "integer", "minimum": 0},
            "total": {"type": "integer", "minimum": 1},
            "summary": {"type": "string", "maxLength": 500},
            "session_id": {"type": "string", "maxLength": 300},
            "agent_id": {"type": "string", "maxLength": 300},
            "parent_agent_id": {"type": "string", "maxLength": 300},
        },
    },
}


def _rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_rpc(
    message: Any,
    reporter: Callable[[dict[str, Any]], bool] = post_event,
) -> dict[str, Any] | None:
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
                "serverInfo": {"name": "petcrew", "version": "0.1.0"},
            },
        )
    if method == "ping":
        return _rpc_result(request_id, {})
    if method == "tools/list":
        return _rpc_result(request_id, {"tools": [TOOL_SCHEMA]})
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict) or params.get("name") != TOOL_NAME:
            return _rpc_error(request_id, -32602, "Unknown tool")
        try:
            event = event_from_report(params.get("arguments", {}))
            delivered = bool(reporter(event))
            text = (
                "Статус передан в локальный PetCrew."
                if delivered
                else "PetCrew сейчас не запущен; основная работа продолжается."
            )
            return _rpc_result(
                request_id,
                {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": {"delivered": delivered},
                    "isError": False,
                },
            )
        except ValueError as error:
            return _rpc_result(
                request_id,
                {
                    "content": [{"type": "text", "text": f"Некорректный статус: {error}."}],
                    "structuredContent": {"delivered": False},
                    "isError": True,
                },
            )
        except Exception:
            return _rpc_result(
                request_id,
                {
                    "content": [
                        {"type": "text", "text": "PetCrew недоступен; основная работа продолжается."}
                    ],
                    "structuredContent": {"delivered": False},
                    "isError": False,
                },
            )
    if method == "shutdown":
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


def run_hook(stdin: TextIO = sys.stdin) -> int:
    try:
        raw = stdin.read(MAX_HOOK_BYTES + 1)
        if len(raw) > MAX_HOOK_BYTES:
            return 0
        event = event_from_hook(json.loads(raw))
        if event is not None:
            persist_registry_event(event)
            post_event(event)
    except Exception:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["hook"]:
        return run_hook()
    if arguments == ["mcp"]:
        return run_mcp()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
