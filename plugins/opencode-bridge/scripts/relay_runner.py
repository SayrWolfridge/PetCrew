"""Stream CLI evidence without retaining model output or diagnostic messages."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import subprocess
import threading
from typing import Callable


MAX_LINE_BYTES = 64 * 1024
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_BUSY_PHRASE = "already has an active writer"


@dataclass
class RunEvidence:
    started: bool = False
    terminal_phase: str | None = None
    turn_id: str | None = None
    exit_code: int | None = None
    error_class: str | None = None
    unstarted_busy: bool = False


def _events(stream):
    """Use sized reads even while discarding arbitrarily long JSONL records."""
    discarding = False
    while True:
        line = stream.readline(MAX_LINE_BYTES + 1)
        if not line:
            return
        newline = line.endswith(b"\n") if isinstance(line, bytes) else line.endswith("\n")
        oversized = len(line) > MAX_LINE_BYTES
        if discarding or oversized:
            discarding = not newline
            continue
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if isinstance(event, dict):
            yield event


def _is_busy(event: dict) -> bool:
    # Inspect only structured error text, never model/tool item content.
    error = event.get("error")
    message = error.get("message") if isinstance(error, dict) else event.get("message")
    return isinstance(message, str) and _BUSY_PHRASE in message


_STDERR_INSPECT_CAP = 256 * 1024


def _drain_stderr(stderr_stream) -> bool:
    """Drain stderr to EOF; inspect only a bounded prefix for the busy phrase."""
    if stderr_stream is None:
        return False
    try:
        inspected = bytearray()
        while True:
            chunk = stderr_stream.read(4096)
            if not chunk:
                break
            remaining = _STDERR_INSPECT_CAP - len(inspected)
            if remaining > 0:
                inspected.extend(chunk[:remaining])
    except Exception:
        return False
    if not inspected:
        return False
    try:
        text = bytes(inspected).decode("utf-8", errors="replace")
    except Exception:
        return False
    return _BUSY_PHRASE in text.lower()


def _stop_child(process, evidence: RunEvidence) -> None:
    """Reap the exact child on exceptional paths; never touch other processes."""
    try:
        if process.poll() is None:
            try:
                process.terminate()
            except Exception:
                # Terminate may race with exit or fail; still attempt the child kill
                # and wait, so a callback failure never simply abandons the child.
                process.kill()
        try:
            evidence.exit_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            evidence.exit_code = process.wait()
    except Exception:
        # A failed termination must not hide the need for manual reconciliation.
        evidence.error_class = "process_cleanup_failed"
        evidence.unstarted_busy = False


def run_stream(
    command: list[str], workspace: str, on_started: Callable[[str | None], None],
    *, process_factory=subprocess.Popen,
) -> RunEvidence:
    evidence = RunEvidence()
    try:
        process = process_factory(
            command, cwd=workspace, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        evidence.error_class = "spawn_failed"
        return evidence

    stderr_result: list[bool] = [False]
    stderr_thread: threading.Thread | None = None
    stderr_stream = getattr(process, "stderr", None)
    if stderr_stream is not None:
        def _drain():
            stderr_result[0] = _drain_stderr(stderr_stream)
        stderr_thread = threading.Thread(target=_drain, daemon=True)
        stderr_thread.start()

    operation = "stream_read_failed"
    try:
        if process.stdout is None:
            raise RuntimeError("missing pipe")
        for event in _events(process.stdout):
            kind = event.get("type")
            if kind == "turn.started":
                if not evidence.started:
                    evidence.started = True
                    evidence.unstarted_busy = False
                    evidence.error_class = None
                    turn_id = event.get("turn_id")
                    evidence.turn_id = turn_id if isinstance(turn_id, str) and _UUID.fullmatch(turn_id) else None
                    operation = "callback_failed"
                    on_started(evidence.turn_id)
                    operation = "stream_read_failed"
            elif kind in ("turn.completed", "turn.failed", "turn.cancelled"):
                evidence.terminal_phase = kind.partition(".")[2]
                if kind != "turn.completed":
                    evidence.error_class = "turn_" + evidence.terminal_phase
                    evidence.unstarted_busy = not evidence.started and _is_busy(event)
            elif kind == "error":
                evidence.error_class = "cli_error"
                evidence.unstarted_busy = not evidence.started and _is_busy(event)
        operation = "process_wait_failed"
        evidence.exit_code = process.wait()
        if stderr_thread is not None:
            stderr_thread.join(timeout=10)
        if not evidence.started and not evidence.unstarted_busy:
            evidence.unstarted_busy = stderr_result[0]
        if evidence.exit_code != 0 and evidence.error_class is None:
            evidence.error_class = "cli_exit_nonzero"
    except Exception:
        evidence.error_class = operation
        evidence.unstarted_busy = False
        _stop_child(process, evidence)
    finally:
        if stderr_thread is not None:
            stderr_thread.join(timeout=10)
        if stderr_stream is not None:
            try:
                stderr_stream.close()
            except Exception:
                evidence.error_class = "stream_close_failed"
                evidence.unstarted_busy = False
        if process.stdout is not None:
            try:
                process.stdout.close()
            except Exception:
                evidence.error_class = "stream_close_failed"
                evidence.unstarted_busy = False
    return evidence
