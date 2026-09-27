from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


PROTOCOL_VERSION = "1.0"
TERMINAL_PHASES = {"completed", "failed", "cancelled"}
THREAD_ID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
OPAQUE_SESSION_PATTERN = re.compile(r"^session:[0-9a-f]{64}$")
COMPLETION_ID_PATTERN = re.compile(r"^completion:[0-9a-f]{64}$")
HEX_SECRET_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
RECONNECT_SECONDS = 5.0
MAX_COMPLETION_RECEIPTS = 512
MAX_PENDING_RESULT_READY = 512


def _local_app_data() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    if not value:
        raise RuntimeError("LOCALAPPDATA is not configured")
    return Path(value)


def relay_dir() -> Path:
    return _local_app_data() / "opencode-bridge" / "relay"


def route_dir() -> Path:
    return relay_dir() / "routes"


def binding_dir() -> Path:
    return relay_dir() / "bindings"


def result_ready_dir() -> Path:
    return relay_dir() / "result-ready"


def _opaque_session_id(raw_session_id: str) -> str:
    digest = hashlib.sha256(raw_session_id.strip().encode("utf-8")).hexdigest()
    return f"session:{digest}"


def _route_path(raw_session_id: str) -> Path:
    return route_dir() / f"{_opaque_session_id(raw_session_id).split(':', 1)[1]}.json"


def _binding_path(raw_session_id: str) -> Path:
    return binding_dir() / f"{_opaque_session_id(raw_session_id).split(':', 1)[1]}.json"


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _validate_binding(
    binding: Any,
    opaque_session_id: str,
) -> dict[str, Any] | None:
    if not isinstance(binding, dict):
        return None
    raw_session_id = binding.get("opencode_session_id")
    thread_id = binding.get("codex_thread_id")
    workspace = binding.get("workspace")
    binding_id = binding.get("binding_id")
    after_cursor = binding.get("after_cursor")
    last_attempted_cursor = binding.get("last_attempted_cursor")
    attempted_completion_ids = binding.get("attempted_completion_ids", [])
    delivered_completion_ids = binding.get("delivered_completion_ids", [])
    bound_at = binding.get("bound_at")
    if (
        binding.get("schema_version") != 2
        or not isinstance(raw_session_id, str)
        or not raw_session_id.startswith("ses")
        or binding.get("opaque_session_id") != opaque_session_id
        or THREAD_ID_PATTERN.fullmatch(str(thread_id)) is None
        or not isinstance(workspace, str)
        or not Path(workspace).is_absolute()
        or not isinstance(binding_id, str)
        or not isinstance(after_cursor, int)
        or after_cursor < 0
        or not isinstance(last_attempted_cursor, int)
        or last_attempted_cursor < 0
        or not isinstance(attempted_completion_ids, list)
        or len(attempted_completion_ids) > MAX_COMPLETION_RECEIPTS
        or not all(
            isinstance(value, str) and COMPLETION_ID_PATTERN.fullmatch(value)
            for value in attempted_completion_ids
        )
        or not isinstance(delivered_completion_ids, list)
        or len(delivered_completion_ids) > MAX_COMPLETION_RECEIPTS
        or not all(
            isinstance(value, str) and COMPLETION_ID_PATTERN.fullmatch(value)
            for value in delivered_completion_ids
        )
        or _parse_time(bound_at) is None
    ):
        return None
    if "attempted_completion_ids" not in binding:
        binding["after_cursor"] = max(after_cursor, last_attempted_cursor)
        binding["attempted_completion_ids"] = []
    if "delivered_completion_ids" not in binding:
        binding["delivered_completion_ids"] = []
    return binding


def _legacy_route_as_binding(
    route: Any,
    opaque_session_id: str,
) -> dict[str, Any] | None:
    if not isinstance(route, dict):
        return None
    raw_session_id = route.get("opencode_session_id")
    thread_id = route.get("codex_thread_id")
    workspace = route.get("workspace")
    route_id = route.get("route_id")
    armed_at = route.get("armed_at")
    if (
        route.get("schema_version") != 1
        or not isinstance(raw_session_id, str)
        or not raw_session_id.startswith("ses")
        or route.get("opaque_session_id") != opaque_session_id
        or THREAD_ID_PATTERN.fullmatch(str(thread_id)) is None
        or not isinstance(workspace, str)
        or not Path(workspace).is_absolute()
        or not isinstance(route_id, str)
        or _parse_time(armed_at) is None
    ):
        return None
    return {
        "schema_version": 2,
        "binding_id": route_id,
        "opencode_session_id": raw_session_id,
        "opaque_session_id": opaque_session_id,
        "codex_thread_id": str(thread_id).lower(),
        "workspace": workspace,
        "bound_at": armed_at,
        "after_cursor": 0,
        "last_attempted_cursor": 0,
        "attempted_completion_ids": [],
        "delivered_completion_ids": [],
        "_legacy_route": True,
    }


def _read_binding(opaque_session_id: str) -> dict[str, Any] | None:
    if OPAQUE_SESSION_PATTERN.fullmatch(opaque_session_id) is None:
        return None
    digest = opaque_session_id.split(":", 1)[1]
    try:
        binding = json.loads((binding_dir() / f"{digest}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        binding = None
    valid = _validate_binding(binding, opaque_session_id)
    if valid is not None:
        return valid
    try:
        route = json.loads((route_dir() / f"{digest}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _legacy_route_as_binding(route, opaque_session_id)


def register_binding(
    raw_session_id: str,
    workspace: str,
    codex_thread_id: str,
    *,
    transfer_parent: bool = False,
) -> tuple[dict[str, Any], bool, dict[str, Any] | None]:
    if not isinstance(codex_thread_id, str):
        raise ValueError("valid codex_thread_id is required")
    thread_id = codex_thread_id.strip()
    if THREAD_ID_PATTERN.fullmatch(thread_id) is None:
        raise ValueError("valid codex_thread_id is required")
    if not isinstance(raw_session_id, str) or not raw_session_id.startswith("ses"):
        raise ValueError("valid OpenCode session id is required")
    workspace_path = Path(workspace).resolve(strict=True)
    if not workspace_path.is_dir():
        raise ValueError("workspace must be an existing directory")
    opaque_session_id = _opaque_session_id(raw_session_id)
    previous = _read_binding(opaque_session_id)
    if previous is not None:
        previous_owner = previous["codex_thread_id"]
        if Path(previous["workspace"]).resolve() != workspace_path:
            raise ValueError("OpenCode session is already bound to another workspace")
        if previous_owner != thread_id.lower() and not transfer_parent:
            raise ValueError(
                "OpenCode session is already bound to another Codex task; "
                "explicit transfer_parent=true is required"
            )
        if (
            previous_owner == thread_id.lower()
        ):
            return previous, False, None
    binding = {
        "schema_version": 2,
        "binding_id": secrets.token_hex(16),
        "opencode_session_id": raw_session_id,
        "opaque_session_id": opaque_session_id,
        "codex_thread_id": thread_id.lower(),
        "workspace": str(workspace_path),
        "bound_at": datetime.now(timezone.utc).isoformat(),
        "after_cursor": _read_cursor(),
        "last_attempted_cursor": 0,
        "attempted_completion_ids": [],
        "delivered_completion_ids": [],
    }
    _atomic_json_write(_binding_path(raw_session_id), binding)
    legacy_path = _route_path(raw_session_id)
    try:
        legacy_path.unlink()
    except FileNotFoundError:
        pass
    return binding, True, previous


def rollback_binding(
    raw_session_id: str,
    binding_id: str,
    previous: dict[str, Any] | None,
) -> None:
    path = _binding_path(raw_session_id)
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(current, dict) or current.get("binding_id") != binding_id:
        return None
    if previous is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    else:
        restored = {key: value for key, value in previous.items() if not key.startswith("_")}
        _atomic_json_write(path, restored)


def detach_binding(raw_session_id: str, codex_thread_id: str) -> bool:
    opaque_session_id = _opaque_session_id(raw_session_id)
    binding = _read_binding(opaque_session_id)
    if binding is None:
        return False
    if binding["codex_thread_id"] != codex_thread_id.strip().lower():
        raise ValueError("OpenCode session is bound to another Codex task")
    path = _binding_path(raw_session_id)
    if binding.get("_legacy_route"):
        path = _route_path(raw_session_id)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _completion_is_current(record: dict[str, Any], binding: dict[str, Any]) -> bool:
    completed_at = _parse_time(record.get("completed_at"))
    bound_at = _parse_time(binding.get("bound_at"))
    cursor = record.get("cursor")
    completion_id = record.get("completion_id")
    return (
        completed_at is not None
        and bound_at is not None
        and completed_at >= bound_at
        and isinstance(cursor, int)
        and cursor > binding["after_cursor"]
        and isinstance(completion_id, str)
        and COMPLETION_ID_PATTERN.fullmatch(completion_id) is not None
        and completion_id not in binding["attempted_completion_ids"]
        and completion_id not in binding["delivered_completion_ids"]
    )


def _claim_completion(
    opaque_session_id: str,
    binding_id: str,
    cursor: int,
    completion_id: str,
) -> dict[str, Any] | None:
    binding = _read_binding(opaque_session_id)
    if (
        binding is None
        or binding.get("binding_id") != binding_id
        or cursor <= binding["after_cursor"]
        or completion_id in binding["attempted_completion_ids"]
        or completion_id in binding["delivered_completion_ids"]
    ):
        return None
    claimed = {key: value for key, value in binding.items() if not key.startswith("_")}
    claimed["last_attempted_cursor"] = cursor
    claimed["attempted_completion_ids"] = [
        *claimed["attempted_completion_ids"],
        completion_id,
    ][-MAX_COMPLETION_RECEIPTS:]
    _atomic_json_write(_binding_path(claimed["opencode_session_id"]), claimed)
    if binding.get("_legacy_route"):
        try:
            _route_path(claimed["opencode_session_id"]).unlink()
        except FileNotFoundError:
            pass
    return claimed


def _complete_completion(
    opaque_session_id: str,
    binding_id: str,
    cursor: int,
    completion_id: str,
) -> None:
    binding = _read_binding(opaque_session_id)
    if (
        binding is None
        or binding.get("binding_id") != binding_id
        or completion_id not in binding["attempted_completion_ids"]
    ):
        return
    completed = {key: value for key, value in binding.items() if not key.startswith("_")}
    if completion_id not in completed["delivered_completion_ids"]:
        completed["delivered_completion_ids"] = [
            *completed["delivered_completion_ids"],
            completion_id,
        ][-MAX_COMPLETION_RECEIPTS:]
    completed["after_cursor"] = max(completed["after_cursor"], cursor)
    _atomic_json_write(_binding_path(completed["opencode_session_id"]), completed)


def _find_codex_command() -> list[str]:
    command = shutil.which("codex")
    if not command:
        raise RuntimeError("standalone Codex CLI is not available in PATH")
    path = Path(command)
    if os.name == "nt" and path.suffix.lower() in {".cmd", ".bat"}:
        comspec = os.environ.get("COMSPEC") or str(
            Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
        )
        return [comspec, "/d", "/s", "/c", str(path)]
    return [command]


def _resume_prompt(binding: dict[str, Any], phase: str, completion_id: str) -> str:
    if phase != "completed":
        return (
            "PetCrew Relay received an event-driven OpenCode terminal signal. "
            f"Session {binding['opencode_session_id']} is {phase}. "
            "Use opencode_read_session with "
            f"workspace {json.dumps(binding['workspace'], ensure_ascii=False)} and "
            f"session_id {binding['opencode_session_id']} to inspect the terminal state, "
            "then continue the user's existing task. "
            "Do not resend or duplicate the OpenCode task."
        )
    return (
        "PetCrew Relay received an event-driven OpenCode terminal signal. "
        f"Session {binding['opencode_session_id']} is {phase}. "
        "Use opencode_read_session with "
        f"workspace {json.dumps(binding['workspace'], ensure_ascii=False)} and "
        f"session_id {binding['opencode_session_id']}, "
        f"completion_id {completion_id}, and phase {phase} to read that exact terminal response, "
        "then continue the user's existing task from that result. "
        "Do not resend or duplicate the OpenCode task."
    )


def _default_runner(command: list[str], workspace: str) -> int:
    result = subprocess.run(
        command,
        cwd=workspace,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    return result.returncode


def _append_log(event: str, **fields: Any) -> None:
    path = relay_dir() / "relay.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = {
        "time": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(safe, ensure_ascii=True, separators=(",", ":")) + "\n")


def _safe_project_name(workspace: str) -> str:
    candidate = Path(workspace).name
    cleaned = " ".join(
        "".join(character for character in candidate if character.isprintable()).split()
    )
    return cleaned[:120] or "Проект Codex"


def _result_ready_event(
    binding: dict[str, Any],
    record: dict[str, Any],
    *,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    thread_id = binding["codex_thread_id"].lower()
    completion_id = record["completion_id"]
    event_digest = hashlib.sha256(
        f"{thread_id}\0{completion_id}".encode("utf-8")
    ).hexdigest()
    turn_digest = hashlib.sha256(
        f"opencode-relay\0{completion_id}".encode("utf-8")
    ).hexdigest()
    session_digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
    normalized_workspace = str(Path(binding["workspace"]).resolve()).lower()
    project_digest = hashlib.sha256(normalized_workspace.encode("utf-8")).hexdigest()
    completed_at = occurred_at or datetime.now(timezone.utc).isoformat()
    return {
        "protocol_version": PROTOCOL_VERSION,
        "event_id": f"relay-result:{event_digest}",
        "sequence": max(1, record["cursor"]),
        "occurred_at": completed_at,
        "provider": "codex",
        "session_id": f"session:{session_digest}",
        "agent_id": f"turn:{turn_digest}",
        "parent_agent_id": None,
        "event_type": "agent.discovered",
        "payload": {
            "project": {
                "id": f"project:{project_digest}",
                "name": _safe_project_name(binding["workspace"]),
                "path": None,
            },
            "task": {
                "title": "Результат OpenCode готов",
                "detail": None,
            },
            "phase": "completed",
            "current_action": "Фоновая проверка завершена",
            "result": {
                "summary": "Результат готов — откройте задачу Codex",
                "outcome": "success",
                "completed_at": completed_at,
                "unread": True,
            },
            "navigation": {
                "kind": "task",
                "label": "Открыть в Codex",
                "target": thread_id,
            },
        },
    }


def _result_ready_path(event: dict[str, Any]) -> Path:
    digest = event["event_id"].split(":", 1)[1]
    return result_ready_dir() / f"{digest}.json"


def _valid_result_ready_event(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    payload = event.get("payload")
    result = payload.get("result") if isinstance(payload, dict) else None
    navigation = payload.get("navigation") if isinstance(payload, dict) else None
    return (
        event.get("protocol_version") == PROTOCOL_VERSION
        and isinstance(event.get("event_id"), str)
        and re.fullmatch(r"relay-result:[0-9a-f]{64}", event["event_id"]) is not None
        and isinstance(event.get("sequence"), int)
        and event["sequence"] > 0
        and _parse_time(event.get("occurred_at")) is not None
        and event.get("provider") == "codex"
        and re.fullmatch(r"session:[0-9a-f]{64}", str(event.get("session_id"))) is not None
        and re.fullmatch(r"turn:[0-9a-f]{64}", str(event.get("agent_id"))) is not None
        and event.get("parent_agent_id") is None
        and event.get("event_type") == "agent.discovered"
        and isinstance(payload, dict)
        and payload.get("phase") == "completed"
        and isinstance(result, dict)
        and result.get("unread") is True
        and result.get("outcome") == "success"
        and _parse_time(result.get("completed_at")) is not None
        and isinstance(navigation, dict)
        and navigation.get("kind") == "task"
        and THREAD_ID_PATTERN.fullmatch(str(navigation.get("target"))) is not None
    )


def _send_result_ready(
    event: dict[str, Any],
    endpoint: str,
    secret: str,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> None:
    request = urllib.request.Request(
        endpoint + "/v1/events",
        data=json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with opener(request, timeout=5) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            if status != 202:
                raise RuntimeError("PetCrew rejected the result-ready event")
    except urllib.error.HTTPError as error:
        if error.code != 409:
            raise


def _publish_result_ready(binding: dict[str, Any], record: dict[str, Any]) -> bool:
    event = _result_ready_event(binding, record)
    pending = _result_ready_path(event)
    _atomic_json_write(pending, {"schema_version": 1, "event": event})
    try:
        endpoint, secret = _petcrew_discover()
        _send_result_ready(event, endpoint, secret)
    except Exception as error:
        _append_log(
            "result_ready_pending",
            cursor=record["cursor"],
            error=type(error).__name__,
        )
        return False
    pending.unlink(missing_ok=True)
    _append_log("result_ready_published", cursor=record["cursor"])
    return True


def _flush_pending_result_ready(endpoint: str, secret: str) -> int:
    published = 0
    files = sorted(result_ready_dir().glob("*.json"))[:MAX_PENDING_RESULT_READY]
    for path in files:
        try:
            wrapper = json.loads(path.read_text(encoding="utf-8"))
            event = wrapper.get("event") if isinstance(wrapper, dict) else None
        except (OSError, ValueError):
            event = None
        if not _valid_result_ready_event(event):
            path.unlink(missing_ok=True)
            _append_log("result_ready_invalid")
            continue
        try:
            _send_result_ready(event, endpoint, secret)
        except Exception as error:
            _append_log("result_ready_retry_failed", error=type(error).__name__)
            break
        path.unlink(missing_ok=True)
        published += 1
    if published:
        _append_log("result_ready_replayed", count=published)
    return published


def process_completion(
    record: Any,
    *,
    runner: Callable[[list[str], str], int] = _default_runner,
    command_prefix: list[str] | None = None,
    result_ready_publisher: Callable[[dict[str, Any], dict[str, Any]], bool]
    | None = None,
) -> str:
    if (
        not isinstance(record, dict)
        or record.get("provider") != "opencode"
        or record.get("phase") not in TERMINAL_PHASES
        or not isinstance(record.get("cursor"), int)
        or not isinstance(record.get("session_id"), str)
        or not isinstance(record.get("completion_id"), str)
        or COMPLETION_ID_PATTERN.fullmatch(record["completion_id"]) is None
    ):
        return "invalid"
    binding = _read_binding(record["session_id"])
    if binding is None:
        return "unrouted"
    if not _completion_is_current(record, binding):
        return "stale"
    binding = _claim_completion(
        record["session_id"],
        binding["binding_id"],
        record["cursor"],
        record["completion_id"],
    )
    if binding is None:
        return "stale"
    prefix = command_prefix if command_prefix is not None else _find_codex_command()
    command = [
        *prefix,
        "exec",
        "resume",
        binding["codex_thread_id"],
        _resume_prompt(binding, record["phase"], record["completion_id"]),
    ]
    try:
        return_code = runner(command, binding["workspace"])
    except Exception as error:
        _append_log("resume_failed", cursor=record["cursor"], error=type(error).__name__)
        return "failed"
    if return_code == 0:
        _complete_completion(
            record["session_id"],
            binding["binding_id"],
            record["cursor"],
            record["completion_id"],
        )
        publisher = result_ready_publisher or _publish_result_ready
        try:
            publisher(binding, record)
        except Exception as error:
            _append_log(
                "result_ready_handler_failed",
                cursor=record["cursor"],
                error=type(error).__name__,
            )
        _append_log("resume_completed", cursor=record["cursor"], phase=record["phase"])
        return "resumed"
    _append_log("resume_failed", cursor=record["cursor"], exit_code=return_code)
    return "failed"


def process_completion_record(record: Any) -> str:
    from return_journal import production_dispatcher
    return production_dispatcher().process_event(record)


def process_completion_with_journal(record: Any, *, runner=None, command_prefix=None,
                                    result_ready_publisher=None, status_probe=None) -> str:
    from return_journal import ReturnDispatcher, get_journal, source_status_probe
    import relay_providers
    dispatcher = ReturnDispatcher(get_journal(), runner=runner, command_prefix=command_prefix,
                                  status_probe=status_probe or source_status_probe,
                                  providers=relay_providers)
    return dispatcher.process_event(record)


def drain_waiting_returns(source_task_id: str, *, runner=None, command_prefix=None,
                          result_ready_publisher=None, status_probe=None) -> str:
    from return_journal import ReturnDispatcher, get_journal, source_status_probe
    import relay_providers
    dispatcher = ReturnDispatcher(get_journal(), runner=runner, command_prefix=command_prefix,
                                  status_probe=status_probe or source_status_probe,
                                  providers=relay_providers)
    return dispatcher.drain_source(source_task_id)


def _opaque_session_id_from_source(source_task_id: str) -> str:
    digest = hashlib.sha256(source_task_id.strip().encode("utf-8")).hexdigest()
    return f"session:{digest}"


def _petcrew_discover() -> tuple[str, str]:
    app_dir = (_local_app_data() / "app.petcrew.overlay").resolve()
    descriptor = json.loads((app_dir / "hub-runtime.json").read_text(encoding="utf-8"))
    if not isinstance(descriptor, dict) or descriptor.get("protocol_version") != PROTOCOL_VERSION:
        raise RuntimeError("PetCrew completion protocol 1.0 is unavailable")
    endpoint = descriptor.get("endpoint")
    secret_file = descriptor.get("secret_file")
    if not isinstance(endpoint, str) or not isinstance(secret_file, str):
        raise RuntimeError("PetCrew runtime descriptor is invalid")
    parsed = urllib.parse.urlsplit(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise RuntimeError("PetCrew endpoint is not a plain loopback URL")
    secret_path = Path(secret_file).resolve()
    if secret_path.parent != app_dir:
        raise RuntimeError("PetCrew secret path escaped app-data")
    secret = secret_path.read_text(encoding="utf-8").strip()
    if HEX_SECRET_PATTERN.fullmatch(secret) is None:
        raise RuntimeError("PetCrew bearer secret is invalid")
    return endpoint.rstrip("/"), secret


def _read_cursor() -> int:
    try:
        payload = json.loads((relay_dir() / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    cursor = payload.get("cursor") if isinstance(payload, dict) else None
    return cursor if isinstance(cursor, int) and cursor >= 0 else 0


def _write_cursor(cursor: int) -> None:
    _atomic_json_write(relay_dir() / "state.json", {"schema_version": 1, "cursor": cursor})


class _CompletionDispatcher:
    def __init__(
        self,
        *,
        handler: Callable[[Any], str] = process_completion_record,
        cursor_writer: Callable[[int], None] = _write_cursor,
        max_workers: int = 4,
    ) -> None:
        self._handler = handler
        self._cursor_writer = cursor_writer
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="petcrew-relay-completion",
        )
        self._lock = threading.Lock()
        self._pending: list[dict[str, Any]] = []
        self._cursor_blocked = False

    def submit(self, record: Any) -> None:
        cursor = record.get("cursor") if isinstance(record, dict) else None
        item = {"cursor": cursor, "done": False, "result": None}
        with self._lock:
            self._pending.append(item)
        future = self._executor.submit(self._handler, record)
        future.add_done_callback(lambda completed: self._finish(item, completed))

    def _finish(self, item: dict[str, Any], future: Future[str]) -> None:
        try:
            result = future.result()
        except Exception as error:
            result = "retry"
            _append_log(
                "completion_handler_exception",
                error=type(error).__name__,
            )
        with self._lock:
            item["done"] = True
            item["result"] = result
            while self._pending and self._pending[0]["done"]:
                completed = self._pending.pop(0)
                cursor = completed["cursor"]
                if completed["result"] == "retry":
                    self._cursor_blocked = True
                if isinstance(cursor, int) and not self._cursor_blocked:
                    self._cursor_writer(cursor)
                if completed["result"] == "failed":
                    _append_log("completion_handled_with_failure")

    def close(self) -> None:
        self._executor.shutdown(wait=True)


def _stream_once(stop_event: threading.Event) -> None:
    endpoint, secret = _petcrew_discover()
    _flush_pending_result_ready(endpoint, secret)
    cursor = _read_cursor()
    path = "/v1/completions/stream?" + urllib.parse.urlencode({"after": cursor})
    request = urllib.request.Request(
        endpoint + path,
        headers={"Authorization": f"Bearer {secret}", "Accept": "text/event-stream"},
    )
    dispatcher = _CompletionDispatcher()
    from return_journal import production_dispatcher
    engine = production_dispatcher()
    # Recovery restores durable state and replays presentation only. A transport
    # reconnect is not evidence that a source writer became available, so it must
    # never launch retained work. The exact source terminal event is the sole
    # event-driven trigger for draining WAITING receipts.
    engine.recover()
    try:
        with urllib.request.urlopen(request, timeout=65) as response:
            event_name = ""
            data_lines: list[str] = []
            while not stop_event.is_set():
                try:
                    raw_line = response.readline()
                except TimeoutError:
                    return
                if not raw_line:
                    return
                line = raw_line.decode("utf-8", errors="strict").rstrip("\r\n")
                if line == "":
                    if event_name == "completion" and data_lines:
                        try:
                            record = json.loads("\n".join(data_lines))
                        except ValueError:
                            record = None
                        dispatcher.submit(record)
                    event_name = ""
                    data_lines = []
                elif line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
    finally:
        dispatcher.close()


def run_relay_forever(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            _stream_once(stop_event)
        except (OSError, ValueError, RuntimeError, urllib.error.URLError):
            pass
        stop_event.wait(RECONNECT_SECONDS)
