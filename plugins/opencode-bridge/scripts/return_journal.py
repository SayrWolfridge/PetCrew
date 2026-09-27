from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any, Callable


JOURNAL_VERSION = 2
MAX_JOURNAL_RECORDS = 512
MAX_RECEIPT_SUMMARIES_PER_SOURCE = 64
MAX_STARTUP_SCAN = 4096

RECEIVED = "received"
WAITING = "waiting"
LAUNCHING = "launching"
RUNNING = "running"
FINISHED = "finished"
FAILED = "failed"
UNCERTAIN = "uncertain"

LIFECYCLE_STATES = {RECEIVED, WAITING, LAUNCHING, RUNNING, FINISHED, FAILED, UNCERTAIN}
TERMINAL_LIFECYCLE = {FINISHED, FAILED, UNCERTAIN}
ACTIVE_LIFECYCLE = {WAITING, LAUNCHING, RUNNING}

_VALID_TRANSITIONS: dict[str, set[str]] = {
    RECEIVED: {WAITING, LAUNCHING, FINISHED, FAILED, UNCERTAIN},
    WAITING: {LAUNCHING, WAITING, FINISHED, FAILED},
    LAUNCHING: {WAITING, RUNNING, FINISHED, FAILED, UNCERTAIN},
    RUNNING: {FINISHED, FAILED, UNCERTAIN},
}

VISIBLE_MAP: dict[str, tuple[str, str]] = {
    RECEIVED: ("queued", "\u0412\u043e\u0437\u0432\u0440\u0430\u0442 \u043f\u043e\u043b\u0443\u0447\u0435\u043d"),
    WAITING: (
        "queued",
        "Результат сохранён, ожидает передачи в Codex",
    ),
    LAUNCHING: (
        "queued",
        "\u0417\u0430\u043f\u0443\u0441\u043a\u0430\u0435\u0442 \u043e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0443 \u0432\u043e\u0437\u0432\u0440\u0430\u0442\u0430",
    ),
    RUNNING: ("working", "Codex \u043f\u0440\u0438\u043d\u0438\u043c\u0430\u0435\u0442 \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 <provider>"),
    FINISHED: (
        "completed",
        "\u041e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0430 \u0432\u043e\u0437\u0432\u0440\u0430\u0442\u0430 \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0430 \u2014 \u043e\u0442\u043a\u0440\u044b\u0442\u044c \u0437\u0430\u0434\u0430\u0447\u0443",
    ),
}

FAILED_LABEL = "\u041d\u0435\u0432\u043e\u0437\u043c\u043e\u0436\u043d\u043e \u043e\u0431\u0440\u0430\u0431\u043e\u0442\u0430\u0442\u044c \u0432\u043e\u0437\u0432\u0440\u0430\u0442"
UNCERTAIN_LABEL = (
    "\u041e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0430 \u043c\u043e\u0433\u043b\u0430 \u043d\u0430\u0447\u0430\u0442\u044c\u0441\u044f;"
    " \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438\u0439 \u043f\u043e\u0432\u0442\u043e\u0440 \u043e\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d"
)

REPLAYABLE_LIFECYCLE = {RECEIVED, WAITING, LAUNCHING, RUNNING}


def _local_app_data() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    if not value:
        raise RuntimeError("LOCALAPPDATA is not configured")
    return Path(value)


def journal_dir() -> Path:
    return _local_app_data() / "opencode-bridge" / "relay" / "return-journal"


def outbox_dir() -> Path:
    return _local_app_data() / "opencode-bridge" / "relay" / "status-outbox"


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


def _make_job_id(provider: str, completion_id: str, binding_generation: str) -> str:
    raw = f"{provider}\0{completion_id}\0{binding_generation}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _source_key(source_task_id: str) -> str:
    digest = hashlib.sha256(source_task_id.encode("utf-8")).hexdigest()
    return digest


def _journal_path(source_key: str) -> Path:
    return journal_dir() / f"{source_key}.json"


def _outbox_path(source_key: str, sequence: int) -> Path:
    return outbox_dir() / source_key / f"{sequence:012d}.json"


def _validate_transition(old: str, new: str) -> bool:
    allowed = _VALID_TRANSITIONS.get(old)
    if allowed is None:
        return False
    return new in allowed


class ReturnRecord:
    __slots__ = (
        "job_id",
        "provider",
        "source_task_id",
        "source_key",
        "completion_id",
        "binding_generation",
        "cursor",
        "terminal_phase",
        "workspace",
        "session_id",
        "opencode_session_id",
        "codex_thread_id",
        "lifecycle",
        "created_at",
        "updated_at",
        "launched_at",
        "turn_started_at",
        "turn_id",
        "exit_code",
        "error_class",
        "is_new_turn",
        "sequence",
        "claimed",
        "bookkeeping_closed",
        "agent_id",
        "completed_at",
        "terminal_evidence",
    )

    def __init__(
        self,
        provider: str,
        source_task_id: str,
        completion_id: str,
        binding_generation: str,
        *,
        cursor: int = 0,
        terminal_phase: str = "",
        workspace: str = "",
        session_id: str = "",
        opencode_session_id: str = "",
        codex_thread_id: str = "",
        agent_id: str = "",
        completed_at: str = "",
    ) -> None:
        self.source_key = _source_key(source_task_id)
        self.job_id = _make_job_id(provider, completion_id, binding_generation)
        self.provider = provider
        self.source_task_id = source_task_id
        self.completion_id = completion_id
        self.binding_generation = binding_generation
        self.cursor = cursor
        self.terminal_phase = terminal_phase
        self.workspace = workspace
        self.session_id = session_id
        self.opencode_session_id = opencode_session_id
        self.codex_thread_id = codex_thread_id
        self.lifecycle = RECEIVED
        now = datetime.now(timezone.utc).isoformat()
        self.created_at = now
        self.updated_at = now
        self.launched_at: str | None = None
        self.turn_started_at: str | None = None
        self.turn_id: str | None = None
        self.exit_code: int | None = None
        self.error_class: str | None = None
        self.is_new_turn: bool = False
        self.sequence: int = 0
        self.claimed = False
        self.bookkeeping_closed = False
        self.agent_id = agent_id
        self.completed_at = completed_at
        self.terminal_evidence: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "provider": self.provider,
            "source_task_id": self.source_task_id,
            "completion_id": self.completion_id,
            "binding_generation": self.binding_generation,
            "cursor": self.cursor,
            "terminal_phase": self.terminal_phase,
            "workspace": self.workspace,
            "session_id": self.session_id,
            "opencode_session_id": self.opencode_session_id,
            "codex_thread_id": self.codex_thread_id,
            "lifecycle": self.lifecycle,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "launched_at": self.launched_at,
            "turn_started_at": self.turn_started_at,
            "turn_id": self.turn_id,
            "exit_code": self.exit_code,
            "error_class": self.error_class,
            "is_new_turn": self.is_new_turn,
            "sequence": self.sequence,
            "claimed": self.claimed,
            "bookkeeping_closed": self.bookkeeping_closed,
            "agent_id": self.agent_id,
            "completed_at": self.completed_at,
            "terminal_evidence": self.terminal_evidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReturnRecord":
        record = cls.__new__(cls)
        record.job_id = data["job_id"]
        record.provider = data["provider"]
        record.source_task_id = data["source_task_id"]
        record.source_key = _source_key(data["source_task_id"])
        record.completion_id = data["completion_id"]
        record.binding_generation = data["binding_generation"]
        record.cursor = data.get("cursor", 0)
        record.terminal_phase = data.get("terminal_phase", "")
        record.workspace = data.get("workspace", "")
        record.session_id = data.get("session_id", "")
        record.opencode_session_id = data.get("opencode_session_id", "")
        record.codex_thread_id = data.get("codex_thread_id", "")
        record.lifecycle = data["lifecycle"]
        record.created_at = data["created_at"]
        record.updated_at = data["updated_at"]
        record.launched_at = data.get("launched_at")
        record.turn_started_at = data.get("turn_started_at")
        record.turn_id = data.get("turn_id")
        record.exit_code = data.get("exit_code")
        record.error_class = data.get("error_class")
        record.is_new_turn = data.get("is_new_turn", False)
        record.sequence = data.get("sequence", 0)
        record.claimed = data.get("claimed", False)
        record.bookkeeping_closed = data.get("bookkeeping_closed", False)
        record.agent_id = data.get("agent_id", "")
        record.completed_at = data.get("completed_at", "")
        record.terminal_evidence = data.get("terminal_evidence")
        return record


class SourceJournal:
    def __init__(self, source_key: str) -> None:
        self.source_key = source_key
        self.records: list[ReturnRecord] = []
        self.pending_drain: list[str] = []
        self._next_sequence: int = 1
        self.status_outbox: list[dict[str, Any]] = []

    def next_sequence(self) -> int:
        seq = self._next_sequence
        self._next_sequence += 1
        return seq

    def add(self, record: ReturnRecord) -> None:
        record.sequence = self.next_sequence()
        self.records.append(record)
        self.status_outbox.append(_build_status_event(self.source_key, record))
        self._persist()

    def update(self, record: ReturnRecord) -> None:
        record.updated_at = datetime.now(timezone.utc).isoformat()
        record.sequence = self.next_sequence()
        self.status_outbox.append(_build_status_event(self.source_key, record))
        self._persist()

    def get_by_job(self, job_id: str) -> ReturnRecord | None:
        for record in self.records:
            if record.job_id == job_id:
                return record
        return None

    def get_by_completion(self, completion_id: str) -> ReturnRecord | None:
        for record in reversed(self.records):
            if record.completion_id == completion_id:
                return record
        return None

    def active_records(self) -> list[ReturnRecord]:
        return [r for r in self.records if r.lifecycle in ACTIVE_LIFECYCLE or r.lifecycle == RECEIVED]

    def terminal_records(self) -> list[ReturnRecord]:
        return [r for r in self.records if r.lifecycle in TERMINAL_LIFECYCLE]

    def _trim(self) -> None:
        while len(self.records) > MAX_RECEIPT_SUMMARIES_PER_SOURCE:
            removable = next((r for r in self.records
                              if r.lifecycle in TERMINAL_LIFECYCLE and r.bookkeeping_closed), None)
            if removable is None:
                raise RuntimeError("relay_capacity")
            self.records.remove(removable)

    def _persist(self) -> None:
        path = _journal_path(self.source_key)
        payload = {
            "schema_version": JOURNAL_VERSION,
            "source_key": self.source_key,
            "next_sequence": self._next_sequence,
            "records": [r.to_dict() for r in self.records],
            "pending_drain": self.pending_drain,
            "status_outbox": self.status_outbox,
        }
        _atomic_json_write(path, payload)

    @classmethod
    def load(cls, source_key: str) -> "SourceJournal":
        journal = cls(source_key)
        path = _journal_path(source_key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return journal
        except (OSError, ValueError) as error:
            raise RuntimeError("journal_unreadable") from error
        if (
            not isinstance(data, dict)
            or data.get("source_key") != source_key
        ):
            raise RuntimeError("journal_identity_mismatch")
        journal._next_sequence = data.get("next_sequence", 1)
        journal.status_outbox = data.get("status_outbox", [])
        for record_data in data.get("records", []):
            if isinstance(record_data, dict):
                journal.records.append(ReturnRecord.from_dict(record_data))
        journal.pending_drain = [
            item for item in data.get("pending_drain", []) if isinstance(item, str)
        ]
        return journal


class ReturnJournal:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._journals: dict[str, SourceJournal] = {}
        self._source_locks: dict[str, threading.RLock] = {}
        self._source_locks_guard = threading.Lock()

    def _get_journal(self, source_key: str) -> SourceJournal:
        if source_key not in self._journals:
            self._journals[source_key] = SourceJournal.load(source_key)
        return self._journals[source_key]

    def get_source_lock(self, source_task_id: str) -> threading.RLock:
        source_key = _source_key(source_task_id)
        with self._source_locks_guard:
            lock = self._source_locks.get(source_key)
            if lock is None:
                lock = threading.RLock()
                self._source_locks[source_key] = lock
            return lock

    def intake(
        self,
        provider: str,
        source_task_id: str,
        completion_id: str,
        binding_generation: str,
        *,
        cursor: int = 0,
        terminal_phase: str = "",
        workspace: str = "",
        session_id: str = "",
        opencode_session_id: str = "",
        codex_thread_id: str = "",
        agent_id: str = "",
        completed_at: str = "",
    ) -> ReturnRecord | None:
        with self._lock:
            source_key = _source_key(source_task_id)
            journal = self._get_journal(source_key)

            self._load_all()
            existing = journal.get_by_job(_make_job_id(provider, completion_id, binding_generation))
            if existing is not None:
                return existing

            self._make_room(journal)
            record = ReturnRecord(
                provider, source_task_id, completion_id, binding_generation,
                cursor=cursor,
                terminal_phase=terminal_phase,
                workspace=workspace,
                session_id=session_id,
                opencode_session_id=opencode_session_id,
                codex_thread_id=codex_thread_id,
                agent_id=agent_id,
                completed_at=completed_at,
            )
            journal.add(record)
            self._enforce_global_limit()
            return record

    def transition(
        self,
        job_id: str,
        new_lifecycle: str,
        *,
        turn_id: str | None = None,
        turn_started_at: str | None = None,
        exit_code: int | None = None,
        error_class: str | None = None,
        is_new_turn: bool = False,
        terminal_evidence: str | None = None,
    ) -> ReturnRecord | None:
        with self._lock:
            for journal in self._journals.values():
                record = journal.get_by_job(job_id)
                if record is not None:
                    if not _validate_transition(record.lifecycle, new_lifecycle):
                        return None
                    record.lifecycle = new_lifecycle
                    if turn_id is not None:
                        record.turn_id = turn_id
                    if turn_started_at is not None:
                        record.turn_started_at = turn_started_at
                    if exit_code is not None:
                        record.exit_code = exit_code
                    if error_class is not None:
                        record.error_class = error_class
                    record.is_new_turn = is_new_turn
                    if terminal_evidence is not None:
                        record.terminal_evidence = terminal_evidence
                    if new_lifecycle == LAUNCHING:
                        record.launched_at = datetime.now(timezone.utc).isoformat()
                    if new_lifecycle == RUNNING and turn_started_at:
                        record.turn_started_at = turn_started_at
                    journal.update(record)
                    return record
            return None

    def get_active_for_source(self, source_task_id: str) -> list[ReturnRecord]:
        with self._lock:
            source_key = _source_key(source_task_id)
            journal = self._get_journal(source_key)
            return journal.active_records()

    def get_record(self, job_id: str) -> ReturnRecord | None:
        with self._lock:
            for journal in self._journals.values():
                record = journal.get_by_job(job_id)
                if record is not None:
                    return record
            return None

    def pending_drain_items(self, source_task_id: str) -> list[str]:
        with self._lock:
            source_key = _source_key(source_task_id)
            journal = self._get_journal(source_key)
            return list(journal.pending_drain)

    def clear_pending_drain(self, source_task_id: str) -> None:
        with self._lock:
            source_key = _source_key(source_task_id)
            journal = self._get_journal(source_key)
            journal.pending_drain.clear()
            journal._persist()

    def enqueue_drain(self, source_task_id: str, job_id: str) -> None:
        with self._lock:
            source_key = _source_key(source_task_id)
            journal = self._get_journal(source_key)
            if job_id not in journal.pending_drain:
                journal.pending_drain.append(job_id)
                journal._persist()

    def count_all(self) -> int:
        with self._lock:
            return sum(len(j.records) for j in self._journals.values())

    def _load_all(self) -> None:
        paths = list(journal_dir().glob("*.json"))
        if len(paths) > MAX_STARTUP_SCAN:
            raise RuntimeError("relay_scan_capacity")
        for path in paths:
            self._get_journal(path.stem)

    def _make_room(self, target: SourceJournal) -> None:
        def remove_one(journals: list[SourceJournal]) -> bool:
            for item in journals:
                removable = next((r for r in item.records
                                  if r.lifecycle in TERMINAL_LIFECYCLE and r.bookkeeping_closed), None)
                if removable is not None:
                    item.records.remove(removable)
                    item._persist()
                    return True
            return False
        while len(target.records) >= MAX_RECEIPT_SUMMARIES_PER_SOURCE:
            if not remove_one([target]):
                raise RuntimeError("relay_source_capacity")
        while self.count_all() >= MAX_JOURNAL_RECORDS:
            if not remove_one(list(self._journals.values())):
                raise RuntimeError("relay_global_capacity")

    def _enforce_global_limit(self) -> None:
        if self.count_all() > MAX_JOURNAL_RECORDS:
            raise RuntimeError("relay_global_capacity")

    def mark_bookkeeping(self, job_id: str, *, claimed: bool = False,
                         closed: bool = False) -> None:
        with self._lock:
            record = self.get_record(job_id)
            if record is None:
                raise RuntimeError("unknown_return")
            record.claimed = record.claimed or claimed
            record.bookkeeping_closed = record.bookkeeping_closed or closed
            self._get_journal(record.source_key)._persist()

    def all_records(self) -> list[ReturnRecord]:
        with self._lock:
            self._load_all()
            return [r for j in self._journals.values() for r in j.records]

    def reconcile_startup(self, source_task_id: str) -> list[ReturnRecord]:
        with self._lock:
            source_key = _source_key(source_task_id)
            journal = self._get_journal(source_key)
            reconciled: list[ReturnRecord] = []
            for record in journal.active_records():
                if record.lifecycle in {LAUNCHING, RUNNING}:
                    if _validate_transition(record.lifecycle, UNCERTAIN):
                        self.transition(record.job_id, UNCERTAIN, error_class="process_restart")
                        reconciled.append(record)
            if reconciled:
                journal._persist()
            return reconciled

    def startup_scan(self) -> list[ReturnRecord]:
        with self._lock:
            self._load_all()
            sources = {r.source_task_id for j in self._journals.values() for r in j.records}
            reconciled = []
            for source in sources:
                reconciled.extend(self.reconcile_startup(source))
            return reconciled


_GLOBAL_JOURNAL = ReturnJournal()


def get_journal() -> ReturnJournal:
    return _GLOBAL_JOURNAL


class StatusOutbox:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def publish(
        self,
        source_task_id: str,
        record: ReturnRecord,
        *,
        publisher: Callable[[dict[str, Any]], tuple[int, str]] | None = None,
    ) -> tuple[int, str]:
        source_key = _source_key(source_task_id)
        event = _build_status_event(source_key, record)
        path = _outbox_path(source_key, record.sequence)
        _atomic_json_write(path, {"schema_version": 1, "event": event})
        if publisher is not None:
            try:
                status, body = publisher(event)
                if status in (202, 409):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    return status, body
                return status, body
            except Exception as error:
                return 500, type(error).__name__
        return 202, "queued"

    def replay_on_reconnect(
        self,
        publisher: Callable[[dict[str, Any]], tuple[int, str]],
    ) -> int:
        with self._lock:
            return self._replay_locked(publisher)

    def _replay_locked(self, publisher) -> int:
        published = 0
        outbox_root = outbox_dir()
        if not outbox_root.exists():
            return 0
        for source_dir in sorted(outbox_root.iterdir())[:MAX_STARTUP_SCAN]:
            if not source_dir.is_dir():
                continue
            for path in sorted(source_dir.glob("*.json")):
                try:
                    wrapper = json.loads(path.read_text(encoding="utf-8"))
                    event = wrapper.get("event") if isinstance(wrapper, dict) else None
                except (OSError, ValueError):
                    event = None
                if not isinstance(event, dict):
                    path.unlink(missing_ok=True)
                    continue
                try:
                    status, _body = publisher(event)
                except Exception:
                    break
                if status in (202, 409):
                    path.unlink(missing_ok=True)
                    published += 1
                else:
                    break
        return published


def _build_status_event(source_key: str, record: ReturnRecord) -> dict[str, Any]:
    agent_id = relay_agent_id(record.source_task_id)
    session_id = relay_session_id(record.source_task_id)
    phase, label = visible_state(record.lifecycle, record.provider)
    if (
        record.lifecycle == FINISHED
        and record.terminal_evidence == "monitor_pickup_ready"
    ):
        label = "Результат OpenCode сохранён — напишите задаче забрать его"
    if record.lifecycle == WAITING and record.error_class == "unstarted_busy":
        label = "Codex пока не принимает возврат; результат сохранён"
    now = datetime.now(timezone.utc).isoformat()
    return {
        "protocol_version": "1.0",
        "event_id": f"relay-status:{source_key}:{record.sequence}",
        "sequence": record.sequence,
        "occurred_at": record.updated_at or now,
        "provider": "codex",
        "session_id": session_id,
        "agent_id": agent_id,
        "parent_agent_id": None,
        "event_type": "agent.discovered",
        "payload": {
            "project": {"id": f"project:{source_key}", "name": "Relay Return", "path": None},
            "task": {"title": label, "detail": None},
            "phase": phase,
            "started_at": record.launched_at or record.created_at,
            "current_action": label,
            "result": {
                "summary": label,
                "outcome": "success" if record.lifecycle == FINISHED else "failure",
                "completed_at": record.updated_at or now,
                "unread": True,
            } if record.lifecycle in TERMINAL_LIFECYCLE else None,
            "navigation": {
                "kind": "task",
                "label": "\u041e\u0442\u043a\u0440\u044b\u0442\u044c \u0432 Codex",
                "target": record.source_task_id,
            },
            **({"return_receipt": {
                "session_id": record.opencode_session_id,
                "completion_id": record.completion_id,
                "phase": record.terminal_phase,
                "workspace": record.workspace,
            }} if (
                record.provider == "opencode"
                and record.lifecycle == FINISHED
                and record.terminal_evidence == "monitor_pickup_ready"
                and record.terminal_phase == "completed"
                and record.opencode_session_id.startswith("ses_")
            ) else {}),
        },
    }


def source_status_probe(
    source_task_id: str,
    *,
    client_factory: Callable[[], Any] | None = None,
) -> str:
    if client_factory is None:
        try:
            from codex_app_reader import CodexAppClient, _find_codex_app_server_command

            client_factory = lambda: CodexAppClient(
                command=_find_codex_app_server_command()
            )
        except Exception:
            return "unavailable"

    try:
        from codex_app_reader import capture_thread_anchor

        anchor = capture_thread_anchor(
            source_task_id, client_factory=client_factory
        )
    except Exception:
        # Reader failures establish no model activity. Desktop can retain writer
        # ownership even after a completed turn.
        return "unavailable"

    turn_status = anchor.get("turn_status")
    turn_id = anchor.get("turn_id")

    if turn_id is None:
        return "idle"

    if isinstance(turn_status, str):
        terminal_statuses = {"completed", "failed", "cancelled", "interrupted"}
        if turn_status.lower() in terminal_statuses:
            return "idle"

    return "active"




def parse_jsonl_events(lines: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "started": False,
        "turn_id": None,
        "turn_started_at": None,
        "terminal": False,
        "terminal_phase": None,
        "exit_code": None,
        "error_class": None,
        "model_text_bytes": 0,
    }
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type", "")
        if event_type == "thread.started":
            pass
        elif event_type == "turn.started":
            result["started"] = True
            result["turn_id"] = event.get("turn_id")
            result["turn_started_at"] = event.get("started_at")
        elif event_type == "turn.delta":
            text = event.get("text", "")
            if isinstance(text, str):
                result["model_text_bytes"] += len(text.encode("utf-8"))
        elif event_type in ("turn.completed", "turn.failed", "turn.cancelled"):
            result["terminal"] = True
            result["terminal_phase"] = event_type.split(".")[1]
        elif event_type == "error":
            result["terminal"] = True
            result["terminal_phase"] = "failed"
            result["error_class"] = event.get("error_class", "unknown")
    return result


class ReturnDispatcher:
    """One execution engine for both providers; publication never launches work."""

    def __init__(self, journal=None, *, status_probe=source_status_probe,
                 runner=None, command_prefix=None, on_state_change=None,
                 outbox=None, providers=None, publisher=None,
                 monitor_pickup_providers=None):
        from relay_runner import run_stream
        self._journal = journal or get_journal()
        self._status_probe = status_probe
        self._runner = runner or run_stream
        self._command_prefix = command_prefix
        self._on_state_change = on_state_change
        self._outbox = outbox or StatusOutbox()
        self._providers = providers
        self._publisher = publisher
        self._monitor_pickup_providers = frozenset(monitor_pickup_providers or ())
        self._recovered = False
        self._wake_requested = set()

    def publish_pending(self):
        # Transition and envelope were one atomic journal write. Stage externally
        # before clearing the WAL envelope, so either side of a crash is replayable.
        with self._journal._lock:
            for source in self._journal._journals.values():
                while source.status_outbox:
                    event = source.status_outbox[0]
                    path = _outbox_path(source.source_key, event["sequence"])
                    _atomic_json_write(path, {"schema_version": 1, "event": event})
                    source.status_outbox.pop(0)
                    source._persist()
        if self._publisher is not None:
            self._outbox.replay_on_reconnect(self._publisher)

    def _notify(self, record):
        self.publish_pending()
        if self._on_state_change is not None:
            self._on_state_change(record)

    def _transition(self, record, state, **evidence):
        result = self._journal.transition(record.job_id, state, **evidence)
        if result is None:
            raise RuntimeError("invalid_return_transition")
        self._notify(result)
        return result

    def intake_return(self, provider, source_task_id, completion_id,
                      binding_generation, **kwargs):
        with self._journal._lock:
            record = self._journal.intake(provider, source_task_id, completion_id,
                                          binding_generation, **kwargs)
            self._notify(record)
            if record.lifecycle == RECEIVED:
                self._transition(record, WAITING)
            return record

    def _claim(self, record):
        with self._journal._lock:
            if self._providers is not None:
                if not self._providers.validate(record) or not self._providers.claim(record):
                    self._transition(record, FAILED, error_class="stale_binding")
                    self._journal.mark_bookkeeping(record.job_id, closed=True)
                    return False
            self._journal.mark_bookkeeping(record.job_id, claimed=True)
            return True

    def try_launch(self, provider, source_task_id, prompt_builder, workspace,
                   codex_thread_id):
        from relay_runner import RunEvidence
        with self._journal.get_source_lock(source_task_id):
            active = self._journal.get_active_for_source(source_task_id)
            if any(r.lifecycle in {LAUNCHING, RUNNING} for r in active):
                return None
            record = next((r for r in active if r.lifecycle in {RECEIVED, WAITING}), None)
            if record is None:
                return None
            if record.lifecycle == RECEIVED:
                self._transition(record, WAITING)
            if not self._claim(record):
                return record
            if provider in self._monitor_pickup_providers:
                # A Desktop-owned Codex task can retain its writer for the app
                # lifetime. Starting a second app-server status probe and a
                # headless `codex exec resume` cannot prove or share that
                # ownership; it only adds avoidable App Server work. Preserve
                # the exact claimed receipt for an explicit in-task pickup and
                # close this dispatcher job without claiming in-chat delivery.
                record = self._transition(
                    record,
                    FINISHED,
                    terminal_evidence="monitor_pickup_ready",
                )
                self._journal.mark_bookkeeping(record.job_id, closed=True)
                return record
            if self._status_probe(source_task_id) != "idle":
                return None
            # Revalidate after the status read: a transfer may happen during it.
            if self._providers is not None and not self._providers.validate(record):
                self._transition(record, FAILED, error_class="stale_binding")
                self._journal.mark_bookkeeping(record.job_id, closed=True)
                return record
            prefix = self._command_prefix or _find_codex_command()
            prompt = prompt_builder(record)
            self._transition(record, LAUNCHING)
            def started(turn_id):
                self._transition(record, RUNNING, turn_id=turn_id,
                                 turn_started_at=datetime.now(timezone.utc).isoformat())
            try:
                evidence = self._runner(
                    [*prefix, "exec", "resume", "--json", codex_thread_id, prompt],
                    workspace, started)
            except Exception:
                # Injected runner or callback can fail after spawning. Never retry.
                evidence = RunEvidence(error_class="runner_exception")
            if not isinstance(evidence, RunEvidence):
                evidence = RunEvidence(error_class="invalid_runner_evidence")
            if (evidence.started and evidence.terminal_phase == "completed"
                    and evidence.exit_code == 0 and evidence.error_class is None):
                self._transition(record, FINISHED, exit_code=0,
                                 terminal_evidence="completed")
                if self._providers is None or self._providers.complete(record):
                    self._journal.mark_bookkeeping(record.job_id, closed=True)
            elif not evidence.started and evidence.unstarted_busy:
                # Exact structured no-start ownership conflict. A persisted idle
                # snapshot can coexist with Desktop ownership; keep the receipt.
                self._transition(record, WAITING, exit_code=evidence.exit_code,
                                 error_class="unstarted_busy")
            elif (evidence.error_class == "spawn_failed"
                  or (evidence.started and evidence.terminal_phase in {"failed", "cancelled"})):
                self._transition(record, FAILED, exit_code=evidence.exit_code,
                                 error_class=evidence.error_class or "turn_failed",
                                 terminal_evidence=evidence.terminal_phase)
            else:
                self._transition(record, UNCERTAIN, exit_code=evidence.exit_code,
                                 error_class=evidence.error_class or "missing_terminal_evidence")
            return record

    def drain_on_terminal(self, provider, source_task_id, prompt_builder, workspace,
                          codex_thread_id):
        results = []
        while True:
            record = self.try_launch(provider, source_task_id,
                                     lambda r: prompt_builder([r]), workspace, codex_thread_id)
            if record is None:
                break
            results.append(record)
            if record.lifecycle in {WAITING, UNCERTAIN}:
                break
        return results

    def reconcile_startup(self, source_task_id):
        result = self._journal.reconcile_startup(source_task_id)
        self.publish_pending()
        return result

    def recover(self):
        records = self._journal.all_records()
        if not self._recovered:
            self._journal.startup_scan()
            self._recovered = True
        # Close finished-but-not-delivered crash window without running a model.
        for record in records:
            if record.lifecycle == FINISHED and not record.bookkeeping_closed:
                if self._providers is not None and self._providers.complete(record):
                    self._journal.mark_bookkeeping(record.job_id, closed=True)
        self.publish_pending()
        return sorted({r.source_task_id for r in records
                       if r.lifecycle in {RECEIVED, WAITING}})

    def drain_source(self, source_task_id):
        lock = self._journal.get_source_lock(source_task_id)
        with self._journal._lock:
            if not lock.acquire(blocking=False):
                self._wake_requested.add(source_task_id)
                return "waiting"
        released = False
        try:
            last = None
            while True:
                with self._journal._lock:
                    self._wake_requested.discard(source_task_id)
                    pending = self._journal.get_active_for_source(source_task_id)
                    next_record = next((r for r in pending if r.lifecycle in {RECEIVED, WAITING}), None)
                    if next_record is None:
                        lock.release()
                        released = True
                        break
                last = self.try_launch(next_record.provider, source_task_id,
                                       self._providers.prompt, next_record.workspace,
                                       source_task_id)
                if last is None or last.lifecycle in {WAITING, UNCERTAIN}:
                    with self._journal._lock:
                        if source_task_id in self._wake_requested:
                            continue
                        lock.release()
                        released = True
                        break
            if last is not None and last.lifecycle == FINISHED:
                if last.terminal_evidence == "monitor_pickup_ready":
                    return "pickup_ready"
                return "resumed"
            if last is not None and last.lifecycle in {FAILED, UNCERTAIN}:
                return "failed"
            return "waiting" if pending else "none"
        finally:
            if not released:
                with self._journal._lock:
                    lock.release()

    def process_event(self, event):
        if not isinstance(event, dict):
            return "invalid"
        # Source terminal is an event-driven drain signal even when not a child receipt.
        for source in {r.source_task_id for r in self._journal.all_records()
                       if r.lifecycle in {RECEIVED, WAITING}}:
            if self._providers.source_matches_event(source, event):
                self.drain_source(source)
        route = self._providers.lookup_event(event)
        if route is None:
            return "unrouted"
        with self._journal._lock:
            record = self.intake_return(**route)
            if record.lifecycle in TERMINAL_LIFECYCLE:
                return "stale"
            # WAL -> provider claim -> claimed marker under the journal transaction lock.
            if not self._claim(record):
                return "stale"
        return self.drain_source(record.source_task_id)


def _status_publisher(event):
    import opencode_relay
    endpoint, secret = opencode_relay._petcrew_discover()
    opencode_relay._send_result_ready(event, endpoint, secret)
    return 202, ""


_PRODUCTION_DISPATCHER = None


def production_dispatcher():
    global _PRODUCTION_DISPATCHER
    import relay_providers
    journal = get_journal()
    if _PRODUCTION_DISPATCHER is None or _PRODUCTION_DISPATCHER._journal is not journal:
        _PRODUCTION_DISPATCHER = ReturnDispatcher(
            journal,
            providers=relay_providers,
            publisher=_status_publisher,
            monitor_pickup_providers={"opencode"},
        )
    return _PRODUCTION_DISPATCHER


def _find_codex_command() -> list[str]:
    import shutil

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


def relay_agent_id(source_task_id: str) -> str:
    digest = hashlib.sha256(source_task_id.encode("utf-8")).hexdigest()
    return f"relay:{digest}"


def relay_session_id(source_task_id: str) -> str:
    digest = hashlib.sha256(source_task_id.encode("utf-8")).hexdigest()
    return f"session:{digest}"


def visible_state(lifecycle: str, provider: str = "codex") -> tuple[str, str]:
    if lifecycle == FAILED:
        return "failed", FAILED_LABEL
    if lifecycle == UNCERTAIN:
        return "failed", UNCERTAIN_LABEL
    phase, label = VISIBLE_MAP.get(lifecycle, ("queued", lifecycle))
    return phase, label.replace("<provider>", provider)
