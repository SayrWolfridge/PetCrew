from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable


APP_SERVER_TIMEOUT_SECONDS = 20.0
MAX_TURN_PAGES = 20
TURN_PAGE_SIZE = 100
MAX_VISIBLE_TURNS = 100
MAX_VISIBLE_ITEMS_PER_TURN = 200
MAX_VISIBLE_STRING_CHARS = 12000
MAX_VISIBLE_LIST_ITEMS = 200
MAX_VISIBLE_DICT_KEYS = 100


def _find_codex_app_server_command() -> list[str]:
    if os.name == "nt":
        app_data = os.environ.get("APPDATA")
        node = shutil.which("node")
        if app_data and node:
            codex_js = (
                Path(app_data)
                / "npm"
                / "node_modules"
                / "@openai"
                / "codex"
                / "bin"
                / "codex.js"
            )
            if codex_js.is_file():
                return [node, str(codex_js), "app-server", "--stdio"]

    command = shutil.which("codex")
    if not command:
        raise RuntimeError("standalone Codex CLI is not available in PATH")
    path = Path(command)
    if os.name == "nt" and path.suffix.lower() in {".cmd", ".bat"}:
        comspec = os.environ.get("COMSPEC") or str(
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "cmd.exe"
        )
        return [comspec, "/d", "/s", "/c", str(path), "app-server", "--stdio"]
    return [command, "app-server", "--stdio"]


class CodexAppClient:
    def __init__(
        self,
        *,
        command: list[str] | None = None,
        timeout: float = APP_SERVER_TIMEOUT_SECONDS,
    ) -> None:
        self._timeout = timeout
        self._next_id = 0
        self._responses: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self._process = subprocess.Popen(
            command or _find_codex_app_server_command(),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        try:
            initialized = self.request(
                "initialize",
                {
                    "clientInfo": {"name": "opencode-bridge", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
        except BaseException:
            self.close()
            raise
        if not isinstance(initialized, dict):
            self.close()
            raise RuntimeError("Codex app-server returned an invalid initialize response")
        self.notify("initialized", {})

    def __enter__(self) -> "CodexAppClient":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def _read_stdout(self) -> None:
        stdout = self._process.stdout
        if stdout is None:
            self._responses.put(RuntimeError("Codex app-server stdout is unavailable"))
            return
        try:
            for raw_line in stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError as error:
                    self._responses.put(
                        RuntimeError(f"Codex app-server emitted invalid JSON: {error}")
                    )
                    continue
                if isinstance(message, dict) and "id" in message:
                    self._responses.put(message)
        finally:
            self._responses.put(RuntimeError("Codex app-server closed its output"))

    def _write(self, payload: dict[str, Any]) -> None:
        stdin = self._process.stdin
        if stdin is None or self._process.poll() is not None:
            raise RuntimeError("Codex app-server is not running")
        stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        stdin.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        self._next_id += 1
        request_id = self._next_id
        request_timeout = self._timeout if timeout is None else timeout
        if request_timeout <= 0:
            raise ValueError("request timeout must be positive")
        self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        while True:
            try:
                response = self._responses.get(timeout=request_timeout)
            except queue.Empty as error:
                raise RuntimeError(
                    f"Codex app-server timed out during {method}"
                ) from error
            if isinstance(response, BaseException):
                raise response
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise RuntimeError(
                    f"Codex app-server {method} failed: "
                    f"{json.dumps(response['error'], ensure_ascii=False)}"
                )
            return response.get("result")

    def close(self) -> None:
        if self._process.stdin is not None:
            try:
                self._process.stdin.close()
            except OSError:
                pass
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=5)
        self._reader.join(timeout=1)
        if self._process.stdout is not None:
            self._process.stdout.close()


def _item_hash(item: Any) -> str:
    encoded = json.dumps(
        item,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _find_rollout_path(thread_id: str) -> Path:
    sessions = Path.home() / ".codex" / "sessions"
    if not sessions.is_dir():
        raise RuntimeError("Codex sessions directory is unavailable")
    matches = list(sessions.rglob(f"*{thread_id}.jsonl"))
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one persisted rollout for Codex task {thread_id}, found {len(matches)}"
        )
    return matches[0].resolve()


def _item_hashes(items: Any) -> dict[str, str]:
    if not isinstance(items, list):
        return {}
    hashes: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        hashes[item["id"]] = _item_hash(item)
    return hashes


def _bounded(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) <= MAX_VISIBLE_STRING_CHARS:
            return value
        omitted = len(value) - MAX_VISIBLE_STRING_CHARS
        return value[:MAX_VISIBLE_STRING_CHARS] + f"\n...[{omitted} chars omitted]"
    if isinstance(value, list):
        visible = [_bounded(item) for item in value[:MAX_VISIBLE_LIST_ITEMS]]
        if len(value) > MAX_VISIBLE_LIST_ITEMS:
            visible.append({"omitted_items": len(value) - MAX_VISIBLE_LIST_ITEMS})
        return visible
    if isinstance(value, dict):
        visible: dict[str, Any] = {}
        for key in list(value)[:MAX_VISIBLE_DICT_KEYS]:
            visible[str(key)] = _bounded(value[key])
        if len(value) > MAX_VISIBLE_DICT_KEYS:
            visible["omitted_keys"] = len(value) - MAX_VISIBLE_DICT_KEYS
        return visible
    return value


def _visible_items(items: Any) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(items, list):
        return [], 0
    visible = []
    omitted = 0
    for item in items:
        if not isinstance(item, dict):
            omitted += 1
            continue
        if item.get("type") == "reasoning":
            omitted += 1
            continue
        if len(visible) >= MAX_VISIBLE_ITEMS_PER_TURN:
            omitted += 1
            continue
        visible.append(_bounded(item))
    return visible, omitted


def _latest_turn(
    thread_id: str,
    *,
    client_factory: Callable[[], Any] = CodexAppClient,
) -> dict[str, Any] | None:
    with client_factory() as client:
        loaded = client.request(
            "thread/read",
            {
                "threadId": thread_id,
                "includeTurns": False,
            },
        )
        thread = loaded.get("thread") if isinstance(loaded, dict) else None
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise RuntimeError("Codex app-server returned the wrong target task")
        result = client.request(
            "thread/turns/list",
            {
                "threadId": thread_id,
                "limit": 1,
                "sortDirection": "desc",
                "itemsView": "full",
            },
        )
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, list):
        raise RuntimeError("Codex app-server returned an invalid turn list")
    if not data:
        return None
    turn = data[0]
    if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
        raise RuntimeError("Codex app-server returned an invalid latest turn")
    return turn


def capture_thread_anchor(
    thread_id: str,
    *,
    client_factory: Callable[[], Any] = CodexAppClient,
) -> dict[str, Any]:
    turn = _latest_turn(thread_id, client_factory=client_factory)
    if turn is None:
        return {
            "turn_id": None,
            "turn_status": None,
            "item_hashes": {},
            "visible_items": [],
            "omitted_items": 0,
        }
    visible, omitted = _visible_items(turn.get("items"))
    return {
        "turn_id": turn["id"],
        "turn_status": turn.get("status"),
        "item_hashes": _item_hashes(turn.get("items")),
        "visible_items": visible,
        "omitted_items": omitted,
    }


def read_thread_delta(
    thread_id: str,
    anchor: dict[str, Any],
    *,
    client_factory: Callable[[], Any] = CodexAppClient,
) -> dict[str, Any]:
    baseline_turn_id = anchor.get("turn_id")
    baseline_hashes = anchor.get("item_hashes")
    if baseline_turn_id is not None and not isinstance(baseline_turn_id, str):
        raise ValueError("invalid baseline turn id")
    if not isinstance(baseline_hashes, dict):
        raise ValueError("invalid baseline item hashes")

    newer_desc: list[dict[str, Any]] = []
    baseline_delta: dict[str, Any] | None = None
    found_baseline = baseline_turn_id is None
    cursor: str | None = None
    thread_view: dict[str, Any] = {}

    with client_factory() as client:
        thread_result = client.request(
            "thread/read",
            {
                "threadId": thread_id,
                "includeTurns": False,
            },
        )
        thread = thread_result.get("thread") if isinstance(thread_result, dict) else None
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise RuntimeError("Codex app-server returned the wrong target task")
        for key in ("id", "name", "title", "status", "updatedAt"):
            if key in thread:
                thread_view[key] = _bounded(thread[key])

        for _page in range(MAX_TURN_PAGES):
            params: dict[str, Any] = {
                "threadId": thread_id,
                "limit": TURN_PAGE_SIZE,
                "sortDirection": "desc",
                "itemsView": "full",
            }
            if cursor is not None:
                params["cursor"] = cursor
            result = client.request("thread/turns/list", params)
            data = result.get("data") if isinstance(result, dict) else None
            if not isinstance(data, list):
                raise RuntimeError("Codex app-server returned an invalid turn page")
            for turn in data:
                if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                    raise RuntimeError("Codex app-server returned an invalid turn")
                if turn["id"] == baseline_turn_id:
                    found_baseline = True
                    changed_items = []
                    for item in turn.get("items") or []:
                        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                            continue
                        if baseline_hashes.get(item["id"]) != _item_hash(item):
                            changed_items.append(item)
                    visible, omitted = _visible_items(changed_items)
                    if visible or omitted:
                        baseline_delta = {
                            "turn_id": turn["id"],
                            "status": turn.get("status"),
                            "started_at": turn.get("startedAt"),
                            "completed_at": turn.get("completedAt"),
                            "items": visible,
                            "omitted_items": omitted,
                            "continued_baseline_turn": True,
                        }
                    break
                newer_desc.append(turn)
                if len(newer_desc) > MAX_VISIBLE_TURNS:
                    raise RuntimeError(
                        "Codex return delta exceeds the bounded turn limit; fresh manual review is required"
                    )
            if found_baseline:
                break
            cursor = result.get("nextCursor") if isinstance(result, dict) else None
            if cursor is None:
                break

    if not found_baseline:
        raise RuntimeError(
            "Codex return baseline was not found; refusing to report an ambiguous task delta"
        )

    visible_turns: list[dict[str, Any]] = []
    if baseline_delta is not None:
        visible_turns.append(baseline_delta)
    for turn in reversed(newer_desc):
        visible, omitted = _visible_items(turn.get("items"))
        visible_turns.append(
            {
                "turn_id": turn["id"],
                "status": turn.get("status"),
                "started_at": turn.get("startedAt"),
                "completed_at": turn.get("completedAt"),
                "items": visible,
                "omitted_items": omitted,
                "continued_baseline_turn": False,
            }
        )

    return {
        "target_thread_id": thread_id,
        "thread": thread_view,
        "baseline_turn_id": baseline_turn_id,
        "turn_count": len(visible_turns),
        "turns": visible_turns,
    }
