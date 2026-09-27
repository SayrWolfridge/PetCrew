from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import errno
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from codex_app_reader import capture_thread_anchor

_OS_LOCKING = None
if os.name == "nt":
    try:
        import msvcrt as _OS_LOCKING
    except ImportError:
        pass
else:
    try:
        import fcntl as _OS_LOCKING
    except ImportError:
        pass


class _OSLock:
    """Standard-library OS process-shared lock with thread reentrancy.

    Uses a stable per-binding lock file under the per-install relay namespace.
    The file name contains no PID or thread component so every process and
    thread contends on the same inode.  The lock file is never unlinked on
    release because a concurrent waiter may already hold the old fd.

    Windows uses ``msvcrt.locking`` on byte zero.  Nonblocking attempts are
    retried here so one monotonic deadline covers both thread and OS waits.
    Unix uses ``fcntl.flock`` with the same retry policy.
    """

    def __init__(self, stable_name: str) -> None:
        self._thread_lock = threading.RLock()
        self._count = 0
        self._fd: int | None = None
        self._stable_name = stable_name
        self._owner_thread_id: int | None = None

    def _lock_path(self) -> Path:
        return relay_dir() / "os-locks" / f"{self._stable_name}.lock"

    def _ensure_file(self) -> None:
        if self._fd is not None:
            return
        path = self._lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o666)
        if os.fstat(self._fd).st_size == 0:
            os.write(self._fd, b"\0")

    def _try_os_lock(self) -> bool:
        assert self._fd is not None
        os.lseek(self._fd, 0, os.SEEK_SET)
        if os.name == "nt" and _OS_LOCKING is not None:
            try:
                _OS_LOCKING.locking(self._fd, _OS_LOCKING.LK_NBLCK, 1)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    return False
                raise
            return True
        elif _OS_LOCKING is not None and os.name != "nt":
            try:
                _OS_LOCKING.flock(
                    self._fd, _OS_LOCKING.LOCK_EX | _OS_LOCKING.LOCK_NB
                )
            except BlockingIOError:
                return False
            return True
        else:
            raise OSError("no process-shared locking available on this platform")

    def _os_unlock(self) -> None:
        if self._fd is None:
            return
        os.lseek(self._fd, 0, os.SEEK_SET)
        if os.name == "nt" and _OS_LOCKING is not None:
            _OS_LOCKING.locking(self._fd, _OS_LOCKING.LK_UNLCK, 1)
        elif _OS_LOCKING is not None and os.name != "nt":
            _OS_LOCKING.flock(self._fd, _OS_LOCKING.LOCK_UN)

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if timeout is None:
            timeout = -1
        if timeout < 0:
            thread_acquired = self._thread_lock.acquire(blocking=blocking)
            deadline = None
        elif not blocking:
            thread_acquired = self._thread_lock.acquire(blocking=False)
            deadline = time.monotonic()
        else:
            deadline = time.monotonic() + timeout
            thread_acquired = self._thread_lock.acquire(timeout=timeout)
        if not thread_acquired:
            return False
        self._count += 1
        if self._count == 1:
            try:
                self._ensure_file()
                while not self._try_os_lock():
                    if not blocking:
                        raise TimeoutError("process-shared lock is held")
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError("process-shared lock timed out")
                    delay = 0.05
                    if deadline is not None:
                        delay = min(delay, max(0.0, deadline - time.monotonic()))
                    time.sleep(delay)
            except TimeoutError:
                self._count -= 1
                if self._fd is not None:
                    os.close(self._fd)
                    self._fd = None
                self._thread_lock.release()
                return False
            except Exception:
                self._count -= 1
                if self._fd is not None:
                    os.close(self._fd)
                    self._fd = None
                self._thread_lock.release()
                raise
            self._owner_thread_id = threading.get_ident()
        return True

    def release(self) -> None:
        if self._count <= 0 or self._owner_thread_id != threading.get_ident():
            raise RuntimeError("cannot release un-acquired process-shared lock")
        self._count -= 1
        error = None
        try:
            if self._count == 0:
                try:
                    self._os_unlock()
                except OSError as caught:
                    error = caught
                if self._fd is not None:
                    try:
                        os.close(self._fd)
                    except OSError as caught:
                        error = error or caught
                    self._fd = None
                self._owner_thread_id = None
        finally:
            self._thread_lock.release()
        if error is not None:
            raise error

    def __enter__(self) -> "_OSLock":
        if not self.acquire():
            raise OSError("failed to acquire process-shared lock")
        return self

    def __exit__(self, *args: Any) -> None:
        self.release()


_OS_LOCKS: dict[str, _OSLock] = {}
_OS_LOCKS_GUARD = threading.Lock()


TERMINAL_PHASES = {"completed", "failed", "cancelled"}
THREAD_ID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
OPAQUE_SESSION_PATTERN = re.compile(r"^session:[0-9a-f]{64}$")
OPAQUE_TURN_PATTERN = re.compile(r"^turn:[0-9a-f]{64}$")
COMPLETION_ID_PATTERN = re.compile(r"^completion:[0-9a-f]{64}$")
TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")
BINDING_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
MAX_COMPLETION_RECEIPTS = 512
MAX_PENDING_RETURNS = 64
MAX_TARGET_CURSOR_CHARS = 2048
MAX_BASELINE_ITEMS = 2048
MAX_RECEIPT_ANCHORS = 64
RETURN_STATES = {"prepared", "armed", "consumed", "parent_only"}
_SOURCE_LOCKS: dict[str, threading.RLock] = {}
_SOURCE_LOCKS_GUARD = threading.Lock()
_BINDING_LOCKS: dict[str, threading.RLock] = {}
_BINDING_LOCKS_GUARD = threading.Lock()


def _local_app_data() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    if not value:
        raise RuntimeError("LOCALAPPDATA is not configured")
    return Path(value)


def relay_dir() -> Path:
    return _local_app_data() / "opencode-bridge" / "relay"


def binding_dir() -> Path:
    return relay_dir() / "codex-bindings"


def rollback_dir() -> Path:
    return relay_dir() / "codex-rollbacks"


def pending_dir() -> Path:
    return relay_dir() / "codex-pending"


def _canonical_thread_id(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"valid {label} is required")
    thread_id = value.strip()
    if THREAD_ID_PATTERN.fullmatch(thread_id) is None:
        raise ValueError(f"valid {label} is required")
    return thread_id.lower()


def _valid_target_cursor(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_TARGET_CURSOR_CHARS
        and not any(ord(character) < 32 for character in value)
    )


def _valid_turn_id(value: Any) -> bool:
    return value is None or (
        isinstance(value, str)
        and 0 < len(value) <= 2048
        and not any(ord(character) < 32 for character in value)
    )


def _valid_item_hashes(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and len(value) <= MAX_BASELINE_ITEMS
        and all(
            isinstance(item_id, str)
            and 0 < len(item_id) <= 2048
            and isinstance(digest, str)
            and TOKEN_PATTERN.fullmatch(digest) is not None
            for item_id, digest in value.items()
        )
    )


def _valid_anchor(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and _valid_turn_id(value.get("turn_id"))
        and _valid_item_hashes(value.get("item_hashes"))
    )


def _valid_receipt_anchors(value: Any) -> bool:
    if not isinstance(value, list) or len(value) > MAX_RECEIPT_ANCHORS:
        return False
    for receipt in value:
        if (
            not isinstance(receipt, dict)
            or not isinstance(receipt.get("completion_id"), str)
            or COMPLETION_ID_PATTERN.fullmatch(receipt["completion_id"]) is None
            or receipt.get("phase") not in TERMINAL_PHASES
            or not isinstance(receipt.get("cursor"), int)
            or isinstance(receipt.get("cursor"), bool)
            or receipt["cursor"] < 0
            or not _valid_anchor(receipt.get("anchor"))
            or (
                receipt.get("claimed_at") is not None
                and _parse_time(receipt.get("claimed_at")) is None
            )
        ):
            return False
    return True


def _opaque_thread_id(raw_thread_id: str) -> str:
    canonical = _canonical_thread_id(raw_thread_id, "target_thread_id")
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"session:{digest}"


def _binding_path_for_opaque(opaque_session_id: str) -> Path:
    digest = opaque_session_id.split(":", 1)[1]
    return binding_dir() / f"{digest}.json"


def _binding_path(raw_thread_id: str) -> Path:
    return _binding_path_for_opaque(_opaque_thread_id(raw_thread_id))


def _rollback_path_for_opaque(opaque_session_id: str) -> Path:
    digest = opaque_session_id.split(":", 1)[1]
    return rollback_dir() / f"{digest}.json"


def _rollback_path(raw_thread_id: str) -> Path:
    return _rollback_path_for_opaque(_opaque_thread_id(raw_thread_id))


def _pending_path_for_opaque(opaque_session_id: str) -> Path:
    digest = opaque_session_id.split(":", 1)[1]
    return pending_dir() / f"{digest}.json"


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


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


def _validate_binding(
    binding: Any,
    opaque_session_id: str,
) -> dict[str, Any] | None:
    if not isinstance(binding, dict):
        return None
    target_thread_id = binding.get("target_codex_thread_id")
    source_thread_id = binding.get("source_codex_thread_id")
    workspace = binding.get("workspace")
    binding_id = binding.get("binding_id")
    after_cursor = binding.get("after_cursor")
    last_attempted_cursor = binding.get("last_attempted_cursor")
    attempted_completion_ids = binding.get("attempted_completion_ids")
    delivered_completion_ids = binding.get("delivered_completion_ids")
    schema_version = binding.get("schema_version")
    baseline_anchor = binding.get("baseline_anchor")
    receipt_anchors = binding.get("receipt_anchors")
    try:
        target_thread_id = _canonical_thread_id(target_thread_id, "target_thread_id")
        source_thread_id = _canonical_thread_id(source_thread_id, "source_thread_id")
    except ValueError:
        return None
    if (
        schema_version not in {1, 2, 3}
        or target_thread_id == source_thread_id
        or binding.get("opaque_target_session_id") != opaque_session_id
        or _opaque_thread_id(target_thread_id) != opaque_session_id
        or not isinstance(workspace, str)
        or not Path(workspace).is_absolute()
        or not isinstance(binding_id, str)
        or BINDING_ID_PATTERN.fullmatch(binding_id) is None
        or not _valid_target_cursor(binding.get("target_cursor"))
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
        or _parse_time(binding.get("bound_at")) is None
    ):
        return None
    if schema_version in {2, 3} and (
        not _valid_anchor(baseline_anchor)
        or not _valid_receipt_anchors(receipt_anchors)
    ):
        return None
    if schema_version == 1:
        upgraded = dict(binding)
        upgraded["schema_version"] = 3
        upgraded["baseline_anchor"] = {"turn_id": None, "item_hashes": {}}
        upgraded["receipt_anchors"] = []
        upgraded["return_state"] = "consumed"
        return upgraded
    if schema_version == 2:
        upgraded = dict(binding)
        upgraded["schema_version"] = 3
        upgraded["return_state"] = "consumed"
        return upgraded
    if binding.get("return_state") not in RETURN_STATES:
        return None
    return binding


def _read_binding_diagnostic(
    opaque_session_id: str,
) -> tuple[dict[str, Any] | None, str, str | None]:
    if OPAQUE_SESSION_PATTERN.fullmatch(opaque_session_id) is None:
        return None, "invalid_target", "opaque session id is invalid"
    path = _binding_path_for_opaque(opaque_session_id)
    if not path.exists():
        return None, "unbound", None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        return None, "state_unavailable", type(error).__name__
    except ValueError as error:
        return None, "state_invalid", type(error).__name__
    binding = _validate_binding(payload, opaque_session_id)
    if binding is None:
        return None, "state_invalid", "binding schema validation failed"
    return binding, "bound", None


def _read_binding(opaque_session_id: str) -> dict[str, Any] | None:
    binding, _state, _error = _read_binding_diagnostic(opaque_session_id)
    return binding


def runtime_provenance() -> dict[str, Any]:
    script_path = Path(__file__).resolve()
    runtime_root = script_path.parents[1]
    manifest_path = runtime_root / ".codex-plugin" / "plugin.json"
    version = None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(manifest, dict) and isinstance(manifest.get("version"), str):
            version = manifest["version"]
    except (OSError, ValueError):
        pass
    runtime_exists = script_path.exists() and manifest_path.exists()
    return {
        "plugin_version": version,
        "runtime_root": str(runtime_root),
        "runtime_exists": runtime_exists,
        "runtime_stale": not runtime_exists,
        "relay_state_path": str(relay_dir()),
        "binding_schema_versions": [1, 2, 3],
    }


def _source_resume_lock(source_thread_id: str) -> threading.RLock:
    source_thread_id = _canonical_thread_id(source_thread_id, "source_thread_id")
    return _source_resume_lock_for_opaque(_opaque_thread_id(source_thread_id))


def _source_resume_lock_for_opaque(opaque_source_session_id: str) -> threading.RLock:
    if OPAQUE_SESSION_PATTERN.fullmatch(opaque_source_session_id) is None:
        raise ValueError("valid opaque source session id is required")
    with _SOURCE_LOCKS_GUARD:
        lock = _SOURCE_LOCKS.get(opaque_source_session_id)
        if lock is None:
            lock = threading.RLock()
            _SOURCE_LOCKS[opaque_source_session_id] = lock
        return lock


def _binding_lock_for_opaque(opaque_session_id: str) -> _OSLock:
    if OPAQUE_SESSION_PATTERN.fullmatch(opaque_session_id) is None:
        raise ValueError("valid opaque target session id is required")
    with _OS_LOCKS_GUARD:
        lock = _OS_LOCKS.get(opaque_session_id)
        if lock is None:
            digest = opaque_session_id.split(":", 1)[1]
            lock = _OSLock(stable_name=digest)
            _OS_LOCKS[opaque_session_id] = lock
        return lock


def _read_rollback(opaque_session_id: str) -> dict[str, Any] | None:
    if OPAQUE_SESSION_PATTERN.fullmatch(opaque_session_id) is None:
        return None
    try:
        payload = json.loads(
            _rollback_path_for_opaque(opaque_session_id).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("opaque_target_session_id") != opaque_session_id
        or not isinstance(payload.get("binding_id"), str)
        or BINDING_ID_PATTERN.fullmatch(payload["binding_id"]) is None
        or not isinstance(payload.get("rollback_token"), str)
        or TOKEN_PATTERN.fullmatch(payload["rollback_token"]) is None
        or _parse_time(payload.get("created_at")) is None
    ):
        return None
    previous = payload.get("previous_binding")
    if previous is not None and _validate_binding(previous, opaque_session_id) is None:
        return None
    return payload


def register_binding(
    target_thread_id: str,
    source_thread_id: str,
    workspace: str,
    target_cursor: str,
    after_cursor: int,
    target_anchor: dict[str, Any],
    *,
    transfer_parent: bool = False,
) -> tuple[dict[str, Any], bool, str | None]:
    target_thread_id = _canonical_thread_id(target_thread_id, "target_thread_id")
    source_thread_id = _canonical_thread_id(source_thread_id, "source_thread_id")
    if target_thread_id == source_thread_id:
        raise ValueError("target and source Codex tasks must be different")
    if not _valid_target_cursor(target_cursor):
        raise ValueError("a fresh target_cursor is required")
    if not isinstance(after_cursor, int) or isinstance(after_cursor, bool) or after_cursor < 0:
        raise ValueError("after_cursor must be a non-negative integer")
    if not _valid_anchor(target_anchor):
        raise ValueError("a valid target_anchor is required")
    workspace_path = Path(workspace).resolve(strict=True)
    if not workspace_path.is_dir():
        raise ValueError("workspace must be an existing directory")

    opaque_session_id = _opaque_thread_id(target_thread_id)
    with _binding_lock_for_opaque(opaque_session_id):
        previous = _read_binding(opaque_session_id)
        if previous is not None:
            if Path(previous["workspace"]).resolve() != workspace_path:
                raise ValueError("Codex target is already bound from another workspace")
            if previous["source_codex_thread_id"] != source_thread_id and not transfer_parent:
                raise ValueError(
                    "Codex target is already bound to another source task; "
                    "explicit transfer_parent=true is required"
                )
            if (
                previous["source_codex_thread_id"] == source_thread_id
                and previous["target_cursor"] == target_cursor
                and previous["after_cursor"] >= after_cursor
                and previous["baseline_anchor"] == target_anchor
                and previous["return_state"] in {"prepared", "armed"}
            ):
                return previous, False, None

        rollback_token = secrets.token_hex(32)
        binding = {
            "schema_version": 3,
            "binding_id": secrets.token_hex(16),
            "target_codex_thread_id": target_thread_id,
            "opaque_target_session_id": opaque_session_id,
            "source_codex_thread_id": source_thread_id,
            "workspace": str(workspace_path),
            "target_cursor": target_cursor,
            "baseline_anchor": {
                "turn_id": target_anchor.get("turn_id"),
                "item_hashes": dict(target_anchor.get("item_hashes", {})),
            },
            "receipt_anchors": (
                list(previous["receipt_anchors"]) if previous is not None else []
            ),
            "bound_at": datetime.now(timezone.utc).isoformat(),
            "return_state": "prepared",
            "after_cursor": max(
                after_cursor,
                previous["after_cursor"] if previous is not None else 0,
            ),
            "last_attempted_cursor": (
                previous["last_attempted_cursor"] if previous is not None else 0
            ),
            "attempted_completion_ids": (
                list(previous["attempted_completion_ids"]) if previous is not None else []
            ),
            "delivered_completion_ids": (
                list(previous["delivered_completion_ids"]) if previous is not None else []
            ),
        }
        rollback = {
            "schema_version": 1,
            "opaque_target_session_id": opaque_session_id,
            "binding_id": binding["binding_id"],
            "rollback_token": rollback_token,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "previous_binding": previous,
        }
        _atomic_json_write(_rollback_path(target_thread_id), rollback)
        _atomic_json_write(_binding_path(target_thread_id), binding)
        return binding, True, rollback_token


def confirm_binding(
    target_thread_id: str,
    binding_id: str,
    rollback_token: str,
) -> bool:
    opaque_session_id = _opaque_thread_id(target_thread_id)
    with _binding_lock_for_opaque(opaque_session_id):
        binding = _read_binding(opaque_session_id)
        rollback = _read_rollback(opaque_session_id)
        if (
            binding is None
            or binding.get("binding_id") != binding_id
            or binding.get("return_state") != "prepared"
            or rollback is None
            or rollback.get("binding_id") != binding_id
            or rollback.get("rollback_token") != rollback_token
        ):
            return False
        confirmed = dict(binding)
        confirmed["return_state"] = "armed"
        _atomic_json_write(_binding_path_for_opaque(opaque_session_id), confirmed)
        try:
            _rollback_path_for_opaque(opaque_session_id).unlink()
        except FileNotFoundError:
            return False
        return True


def rollback_binding(
    target_thread_id: str,
    binding_id: str,
    rollback_token: str,
) -> bool:
    opaque_session_id = _opaque_thread_id(target_thread_id)
    with _binding_lock_for_opaque(opaque_session_id):
        binding = _read_binding(opaque_session_id)
        rollback = _read_rollback(opaque_session_id)
        if (
            binding is None
            or binding.get("binding_id") != binding_id
            or binding.get("return_state") != "prepared"
            or rollback is None
            or rollback.get("binding_id") != binding_id
            or rollback.get("rollback_token") != rollback_token
        ):
            return False
        previous = rollback.get("previous_binding")
        path = _binding_path_for_opaque(opaque_session_id)
        if previous is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            _atomic_json_write(path, previous)
        try:
            _rollback_path_for_opaque(opaque_session_id).unlink()
        except FileNotFoundError:
            pass
        return True


def detach_binding(target_thread_id: str, source_thread_id: str) -> bool:
    target_thread_id = _canonical_thread_id(target_thread_id, "target_thread_id")
    source_thread_id = _canonical_thread_id(source_thread_id, "source_thread_id")
    opaque_session_id = _opaque_thread_id(target_thread_id)
    with _binding_lock_for_opaque(opaque_session_id):
        binding = _read_binding(opaque_session_id)
        if binding is None:
            return False
        if binding["source_codex_thread_id"] != source_thread_id:
            raise ValueError("Codex target is bound to another source task")
        try:
            _binding_path_for_opaque(opaque_session_id).unlink()
        except FileNotFoundError:
            return False
        try:
            _rollback_path_for_opaque(opaque_session_id).unlink()
        except FileNotFoundError:
            pass
        return True


def binding_status(target_thread_id: str) -> dict[str, Any] | None:
    opaque_session_id = _opaque_thread_id(target_thread_id)
    binding = _read_binding(opaque_session_id)
    if binding is None:
        return None
    rollback = _read_rollback(opaque_session_id)
    return {
        **binding,
        "pending_confirmation": (
            rollback is not None and rollback.get("binding_id") == binding["binding_id"]
        ),
    }


def binding_status_report(target_thread_id: str) -> dict[str, Any]:
    opaque_session_id = _opaque_thread_id(target_thread_id)
    binding, state, error = _read_binding_diagnostic(opaque_session_id)
    provenance = runtime_provenance()
    if provenance["runtime_stale"]:
        state = "runtime_stale"
    if binding is None:
        return {
            "state": state,
            "binding": None,
            "diagnostic": error,
            "runtime": provenance,
        }
    rollback = _read_rollback(opaque_session_id)
    binding = {
        **binding,
        "pending_confirmation": (
            rollback is not None and rollback.get("binding_id") == binding["binding_id"]
        ),
    }
    attempted = set(binding["attempted_completion_ids"])
    delivered = set(binding["delivered_completion_ids"])
    in_flight = [
        receipt["completion_id"]
        for receipt in binding["receipt_anchors"]
        if receipt["completion_id"] in attempted
        and receipt["completion_id"] not in delivered
    ]
    return {
        "state": state,
        "binding": binding,
        "diagnostic": error,
        "runtime": provenance,
        "in_flight_completion_ids": in_flight,
    }


def get_return_parent(target_thread_id: str) -> dict[str, Any]:
    target_thread_id = _canonical_thread_id(target_thread_id, "target_thread_id")
    opaque_session_id = _opaque_thread_id(target_thread_id)
    binding, state, error = _read_binding_diagnostic(opaque_session_id)
    if binding is None:
        return {"target_thread_id": target_thread_id, "state": state, "diagnostic": error}
    return {
        "target_thread_id": target_thread_id,
        "state": "bound",
        "binding_id": binding["binding_id"],
        "source_thread_id": binding["source_codex_thread_id"],
        "workspace": binding["workspace"],
        "return_state": binding["return_state"],
        "return_armed": binding["return_state"] == "armed",
    }


def transfer_return_parent(
    target_thread_id: str,
    expected_binding_id: str,
    new_source_thread_id: str,
    new_workspace: str,
    *,
    transfer_parent: bool = False,
) -> dict[str, Any]:
    target_thread_id = _canonical_thread_id(target_thread_id, "target_thread_id")
    if not transfer_parent:
        raise ValueError("transfer_parent=true is required")
    if not isinstance(expected_binding_id, str) or BINDING_ID_PATTERN.fullmatch(expected_binding_id) is None:
        raise ValueError("valid expected_binding_id is required")
    new_source_thread_id = _canonical_thread_id(new_source_thread_id, "new_source_thread_id")
    if target_thread_id == new_source_thread_id:
        raise ValueError("target and new source Codex tasks must be different")
    new_workspace_path = Path(new_workspace).resolve(strict=True)
    if not new_workspace_path.is_dir():
        raise ValueError("new_workspace must be an existing directory")

    opaque_session_id = _opaque_thread_id(target_thread_id)
    with _binding_lock_for_opaque(opaque_session_id):
        previous = _read_binding(opaque_session_id)
        if previous is None:
            raise ValueError("target has no active return binding")
        if previous["binding_id"] != expected_binding_id:
            raise ValueError("expected generation is stale; current binding changed")
        if previous["source_codex_thread_id"] == new_source_thread_id:
            if Path(previous["workspace"]).resolve() == new_workspace_path:
                return {
                    "target_thread_id": target_thread_id,
                    "previous_source_thread_id": previous["source_codex_thread_id"],
                    "new_source_thread_id": new_source_thread_id,
                    "previous_workspace": previous["workspace"],
                    "new_workspace": str(new_workspace_path),
                    "old_binding_id": previous["binding_id"],
                    "new_binding_id": previous["binding_id"],
                    "binding_changed": False,
                    "return_state": previous["return_state"],
                    "return_armed": previous["return_state"] == "armed",
                    "already_started": (
                        bool(previous["attempted_completion_ids"])
                        or bool(previous["delivered_completion_ids"])
                    ),
                }
            raise ValueError("same-parent workspace change is rejected")

        new_binding_id = secrets.token_hex(16)
        new_binding = {
            "schema_version": 3,
            "binding_id": new_binding_id,
            "target_codex_thread_id": target_thread_id,
            "opaque_target_session_id": opaque_session_id,
            "source_codex_thread_id": new_source_thread_id,
            "workspace": str(new_workspace_path),
            "target_cursor": previous["target_cursor"],
            "baseline_anchor": {
                "turn_id": previous["baseline_anchor"].get("turn_id"),
                "item_hashes": dict(previous["baseline_anchor"].get("item_hashes", {})),
            },
            "receipt_anchors": list(previous["receipt_anchors"]),
            "bound_at": datetime.now(timezone.utc).isoformat(),
            "return_state": "parent_only",
            "after_cursor": previous["after_cursor"],
            "last_attempted_cursor": previous["last_attempted_cursor"],
            "attempted_completion_ids": list(previous["attempted_completion_ids"]),
            "delivered_completion_ids": list(previous["delivered_completion_ids"]),
        }

        _atomic_json_write(_binding_path_for_opaque(opaque_session_id), new_binding)

        return {
            "target_thread_id": target_thread_id,
            "previous_source_thread_id": previous["source_codex_thread_id"],
            "new_source_thread_id": new_source_thread_id,
            "previous_workspace": previous["workspace"],
            "new_workspace": str(new_workspace_path),
            "old_binding_id": previous["binding_id"],
            "new_binding_id": new_binding_id,
            "binding_changed": True,
            "return_state": "parent_only",
            "return_armed": False,
            "already_started": (
                bool(previous["attempted_completion_ids"])
                or bool(previous["delivered_completion_ids"])
            ),
        }


def return_receipt(
    target_thread_id: str,
    source_thread_id: str,
    workspace: str,
    completion_id: str,
) -> dict[str, Any]:
    target_thread_id = _canonical_thread_id(target_thread_id, "target_thread_id")
    source_thread_id = _canonical_thread_id(source_thread_id, "source_thread_id")
    if not isinstance(completion_id, str) or COMPLETION_ID_PATTERN.fullmatch(completion_id) is None:
        raise ValueError("valid completion_id is required")
    binding = _read_binding(_opaque_thread_id(target_thread_id))
    if binding is None:
        raise ValueError("Codex target has no active return binding")
    if binding["source_codex_thread_id"] != source_thread_id:
        raise ValueError("Codex target is bound to another source task")
    if Path(binding["workspace"]).resolve() != Path(workspace).resolve():
        raise ValueError("Codex target is bound from another workspace")
    attempted = completion_id in binding["attempted_completion_ids"]
    delivered = completion_id in binding["delivered_completion_ids"]
    if not attempted and not delivered:
        raise ValueError("terminal receipt does not belong to this return binding")
    receipt = next(
        (
            item
            for item in reversed(binding["receipt_anchors"])
            if item["completion_id"] == completion_id
        ),
        None,
    )
    if receipt is None:
        raise RuntimeError("terminal receipt has no persisted Codex read anchor")
    return {
        "target_thread_id": target_thread_id,
        "source_thread_id": source_thread_id,
        "completion_id": completion_id,
        "phase": receipt["phase"],
        "cursor": receipt["cursor"],
        "receipt_state": "delivered" if delivered else "attempted_current_resume",
        "anchor": receipt["anchor"],
    }


def _read_pending(opaque_source_session_id: str) -> list[dict[str, Any]]:
    if OPAQUE_SESSION_PATTERN.fullmatch(opaque_source_session_id) is None:
        return []
    try:
        payload = json.loads(
            _pending_path_for_opaque(opaque_source_session_id).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return []
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("opaque_source_session_id") != opaque_source_session_id
        or not isinstance(payload.get("entries"), list)
    ):
        return []
    entries = []
    for entry in payload["entries"][-MAX_PENDING_RETURNS:]:
        record = entry.get("record") if isinstance(entry, dict) else None
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("binding_id"), str)
            and BINDING_ID_PATTERN.fullmatch(entry["binding_id"]) is not None
            and isinstance(record, dict)
            and record.get("provider") == "codex"
            and isinstance(record.get("session_id"), str)
            and OPAQUE_SESSION_PATTERN.fullmatch(record["session_id"]) is not None
            and isinstance(record.get("agent_id"), str)
            and OPAQUE_TURN_PATTERN.fullmatch(record["agent_id"]) is not None
            and isinstance(record.get("completion_id"), str)
            and COMPLETION_ID_PATTERN.fullmatch(record["completion_id"]) is not None
            and record.get("phase") in TERMINAL_PHASES
            and isinstance(record.get("cursor"), int)
            and not isinstance(record.get("cursor"), bool)
            and _parse_time(record.get("completed_at")) is not None
            and _parse_time(entry.get("queued_at")) is not None
        ):
            entries.append(entry)
    return entries


def _write_pending(
    opaque_source_session_id: str,
    entries: list[dict[str, Any]],
) -> None:
    path = _pending_path_for_opaque(opaque_source_session_id)
    if not entries:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    _atomic_json_write(
        path,
        {
            "schema_version": 1,
            "opaque_source_session_id": opaque_source_session_id,
            "entries": entries[-MAX_PENDING_RETURNS:],
        },
    )


def _enqueue_pending(binding: dict[str, Any], record: dict[str, Any]) -> None:
    opaque_source_session_id = _opaque_thread_id(binding["source_codex_thread_id"])
    entries = _read_pending(opaque_source_session_id)
    if any(
        entry["record"]["completion_id"] == record["completion_id"]
        for entry in entries
    ):
        return
    safe_record = {
        key: record[key]
        for key in (
            "cursor",
            "completion_id",
            "provider",
            "session_id",
            "agent_id",
            "phase",
            "completed_at",
        )
    }
    entries.append(
        {
            "binding_id": binding["binding_id"],
            "record": safe_record,
            "queued_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    _write_pending(opaque_source_session_id, entries)


def _completion_is_current(record: dict[str, Any], binding: dict[str, Any]) -> bool:
    completed_at = _parse_time(record.get("completed_at"))
    bound_at = _parse_time(binding.get("bound_at"))
    cursor = record.get("cursor")
    completion_id = record.get("completion_id")
    return (
        binding.get("return_state") in {"prepared", "armed"}
        and record.get("provider") == "codex"
        and record.get("phase") in TERMINAL_PHASES
        and record.get("session_id") == binding["opaque_target_session_id"]
        and isinstance(record.get("agent_id"), str)
        and OPAQUE_TURN_PATTERN.fullmatch(record["agent_id"]) is not None
        and completed_at is not None
        and bound_at is not None
        and completed_at >= bound_at
        and isinstance(cursor, int)
        and not isinstance(cursor, bool)
        and cursor > binding["after_cursor"]
        and isinstance(completion_id, str)
        and COMPLETION_ID_PATTERN.fullmatch(completion_id) is not None
        and completion_id not in binding["attempted_completion_ids"]
        and completion_id not in binding["delivered_completion_ids"]
    )


def _auto_confirm(opaque_session_id: str, binding_id: str) -> None:
    rollback = _read_rollback(opaque_session_id)
    if rollback is None or rollback.get("binding_id") != binding_id:
        return
    try:
        _rollback_path_for_opaque(opaque_session_id).unlink()
    except FileNotFoundError:
        pass


def _claim_completion(record: dict[str, Any], binding_id: str) -> dict[str, Any] | None:
    opaque_session_id = record["session_id"]
    with _binding_lock_for_opaque(opaque_session_id):
        binding = _read_binding(opaque_session_id)
        if (
            binding is None
            or binding.get("binding_id") != binding_id
            or binding.get("return_state") not in {"prepared", "armed"}
            or not _completion_is_current(record, binding)
        ):
            return None
        claimed = dict(binding)
        claimed["return_state"] = "consumed"
        claimed["last_attempted_cursor"] = record["cursor"]
        claimed["attempted_completion_ids"] = [
            *claimed["attempted_completion_ids"],
            record["completion_id"],
        ][-MAX_COMPLETION_RECEIPTS:]
        claimed["receipt_anchors"] = [
            *claimed["receipt_anchors"],
            {
                "completion_id": record["completion_id"],
                "phase": record["phase"],
                "cursor": record["cursor"],
                "claimed_at": datetime.now(timezone.utc).isoformat(),
                "anchor": {
                    "turn_id": claimed["baseline_anchor"].get("turn_id"),
                    "item_hashes": dict(
                        claimed["baseline_anchor"].get("item_hashes", {})
                    ),
                },
            },
        ][-MAX_RECEIPT_ANCHORS:]
        _atomic_json_write(_binding_path_for_opaque(opaque_session_id), claimed)
        _auto_confirm(opaque_session_id, binding_id)
        return claimed


def _complete_completion(record: dict[str, Any], binding_id: str) -> None:
    opaque_session_id = record["session_id"]
    with _binding_lock_for_opaque(opaque_session_id):
        binding = _read_binding(opaque_session_id)
        if (
            binding is None
            or binding.get("binding_id") != binding_id
            or record["completion_id"] not in binding["attempted_completion_ids"]
        ):
            return
        completed = dict(binding)
        if record["completion_id"] not in completed["delivered_completion_ids"]:
            completed["delivered_completion_ids"] = [
                *completed["delivered_completion_ids"],
                record["completion_id"],
            ][-MAX_COMPLETION_RECEIPTS:]
        completed["after_cursor"] = max(completed["after_cursor"], record["cursor"])
        try:
            current_anchor = capture_thread_anchor(completed["target_codex_thread_id"])
        except Exception as error:
            _append_log(
                "codex_anchor_refresh_failed",
                cursor=record["cursor"],
                error=type(error).__name__,
            )
        else:
            completed["baseline_anchor"] = {
                "turn_id": current_anchor.get("turn_id"),
                "item_hashes": dict(current_anchor.get("item_hashes", {})),
            }
        _atomic_json_write(_binding_path_for_opaque(opaque_session_id), completed)


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


def _resume_prompt(binding: dict[str, Any], record: dict[str, Any]) -> str:
    return (
        "PetCrew Relay received the one-shot terminal receipt for a delegated Codex turn. "
        f"The delegated turn in target task {binding['target_codex_thread_id']} ended with "
        f"phase {record['phase']}; this does not mean the persistent target task itself ended. "
        "Read the exact matching delta through the OpenCode & Codex Bridge tool "
        "codex_read_return. Call it once with workspace "
        f"{json.dumps(binding['workspace'], ensure_ascii=False)}, target_thread_id "
        f"{binding['target_codex_thread_id']}, source_thread_id "
        f"{binding['source_codex_thread_id']}, and completion_id "
        f"{record['completion_id']}. Treat receipt_state attempted_current_resume as valid: "
        "delivered becomes visible only after this resumed turn exits. Reconcile the returned "
        "user messages, assistant output, tool outcomes, blockers, and current task status "
        "before continuing the source task. "
        "Do not resend or duplicate the delegated task."
    )


def _retry_prompt(items: list[tuple[dict[str, Any], dict[str, Any]]]) -> str:
    targets = []
    for binding, record in items:
        targets.append(
            "- call codex_read_return with workspace "
            f"{json.dumps(binding['workspace'], ensure_ascii=False)}, target_thread_id "
            f"{binding['target_codex_thread_id']}, source_thread_id "
            f"{binding['source_codex_thread_id']}, completion_id "
            f"{record['completion_id']} (phase {record['phase']})"
        )
    return (
        "PetCrew Relay deferred event-driven Codex returns while this source task was still "
        "finishing its previous turn. That turn is now terminal. Read every exact matching delta "
        "through the listed codex_read_return call. Treat attempted_current_resume as a valid "
        "receipt state; delivered becomes visible only after this resumed turn exits. Reconcile "
        "all returned changes before continuing. Do not resend or duplicate any delegated task.\n"
        + "\n".join(targets)
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


def process_completion(
    record: Any,
    *,
    runner: Callable[[list[str], str], int] = _default_runner,
    command_prefix: list[str] | None = None,
) -> str:
    if (
        not isinstance(record, dict)
        or record.get("provider") != "codex"
        or record.get("phase") not in TERMINAL_PHASES
        or not isinstance(record.get("cursor"), int)
        or isinstance(record.get("cursor"), bool)
        or not isinstance(record.get("session_id"), str)
        or OPAQUE_SESSION_PATTERN.fullmatch(record["session_id"]) is None
        or not isinstance(record.get("agent_id"), str)
        or OPAQUE_TURN_PATTERN.fullmatch(record["agent_id"]) is None
        or not isinstance(record.get("completion_id"), str)
        or COMPLETION_ID_PATTERN.fullmatch(record["completion_id"]) is None
    ):
        return "invalid"
    binding = _read_binding(record["session_id"])
    if binding is None:
        return "unrouted"
    if not _completion_is_current(record, binding):
        return "stale"
    binding = _claim_completion(record, binding["binding_id"])
    if binding is None:
        return "stale"
    prefix = command_prefix if command_prefix is not None else _find_codex_command()
    command = [
        *prefix,
        "exec",
        "resume",
        binding["source_codex_thread_id"],
        _resume_prompt(binding, record),
    ]
    with _source_resume_lock(binding["source_codex_thread_id"]):
        try:
            return_code = runner(command, binding["workspace"])
        except Exception as error:
            try:
                _enqueue_pending(binding, record)
            except OSError:
                _append_log(
                    "codex_resume_failed",
                    cursor=record["cursor"],
                    error=type(error).__name__,
                )
                return "failed"
            _append_log(
                "codex_resume_deferred",
                cursor=record["cursor"],
                error=type(error).__name__,
            )
            return "deferred"
        if return_code == 0:
            _complete_completion(record, binding["binding_id"])
            _append_log(
                "codex_resume_completed",
                cursor=record["cursor"],
                phase=record["phase"],
            )
            return "resumed"
        try:
            _enqueue_pending(binding, record)
        except OSError:
            _append_log(
                "codex_resume_failed",
                cursor=record["cursor"],
                exit_code=return_code,
            )
            return "failed"
        _append_log(
            "codex_resume_deferred",
            cursor=record["cursor"],
            exit_code=return_code,
        )
        return "deferred"


def process_completion_with_journal(record: Any, *, runner=None, command_prefix=None,
                                    status_probe=None) -> str:
    from opencode_relay import process_completion_with_journal as shared
    result = shared(record, runner=runner, command_prefix=command_prefix, status_probe=status_probe)
    return "deferred" if result == "waiting" else result


def retry_pending_for_source(
    source_record: Any,
    *,
    runner: Callable[[list[str], str], int] = _default_runner,
    command_prefix: list[str] | None = None,
) -> str:
    if (
        not isinstance(source_record, dict)
        or source_record.get("provider") != "codex"
        or source_record.get("phase") not in TERMINAL_PHASES
        or not isinstance(source_record.get("session_id"), str)
        or OPAQUE_SESSION_PATTERN.fullmatch(source_record["session_id"]) is None
    ):
        return "invalid"
    opaque_source_session_id = source_record["session_id"]
    with _source_resume_lock_for_opaque(opaque_source_session_id):
        return _retry_pending_locked(
            opaque_source_session_id,
            runner=runner,
            command_prefix=command_prefix,
        )


def _retry_pending_locked(
    opaque_source_session_id: str,
    *,
    runner: Callable[[list[str], str], int],
    command_prefix: list[str] | None,
) -> str:
    entries = _read_pending(opaque_source_session_id)
    if not entries:
        return "none"

    ready: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for entry in entries:
        record = entry["record"]
        binding = _read_binding(record["session_id"])
        if (
            binding is not None
            and binding.get("binding_id") == entry["binding_id"]
            and _opaque_thread_id(binding["source_codex_thread_id"])
            == opaque_source_session_id
            and record["completion_id"] in binding["attempted_completion_ids"]
            and record["completion_id"] not in binding["delivered_completion_ids"]
        ):
            ready.append((binding, record))
    _write_pending(opaque_source_session_id, [])
    if not ready:
        return "none"

    source_thread_id = ready[0][0]["source_codex_thread_id"]
    workspace = ready[0][0]["workspace"]
    ready = [
        item
        for item in ready
        if item[0]["source_codex_thread_id"] == source_thread_id
        and item[0]["workspace"] == workspace
    ]
    prefix = command_prefix if command_prefix is not None else _find_codex_command()
    command = [
        *prefix,
        "exec",
        "resume",
        source_thread_id,
        _retry_prompt(ready),
    ]
    try:
        return_code = runner(command, workspace)
    except Exception as error:
        _append_log(
            "codex_deferred_resume_failed",
            count=len(ready),
            error=type(error).__name__,
        )
        return "failed"
    if return_code != 0:
        _append_log(
            "codex_deferred_resume_failed",
            count=len(ready),
            exit_code=return_code,
        )
        return "failed"
    for binding, record in ready:
        _complete_completion(record, binding["binding_id"])
    _append_log("codex_deferred_resume_completed", count=len(ready))
    return "resumed"
