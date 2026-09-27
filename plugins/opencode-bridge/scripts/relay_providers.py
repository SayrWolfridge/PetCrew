"""Exact provider receipts for the shared dispatcher; no prompt persistence."""
from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
import threading
from typing import Any


_OPENCODE_LOCK = threading.RLock()


def _module(provider: str):
    if provider == "codex":
        import codex_relay
        return codex_relay
    if provider == "opencode":
        import opencode_relay
        return opencode_relay
    return None


def _terminal(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    module = _module(event.get("provider"))
    if module is None:
        return False
    if (event.get("phase") not in module.TERMINAL_PHASES
            or type(event.get("cursor")) is not int or event["cursor"] < 1
            or not isinstance(event.get("session_id"), str)
            or module.OPAQUE_SESSION_PATTERN.fullmatch(event["session_id"]) is None
            or not isinstance(event.get("completion_id"), str)
            or module.COMPLETION_ID_PATTERN.fullmatch(event["completion_id"]) is None):
        return False
    if event["provider"] == "codex":
        return (isinstance(event.get("agent_id"), str)
                and module.OPAQUE_TURN_PATTERN.fullmatch(event["agent_id"]) is not None)
    return True


def _lock(module, event):
    if module is None:
        return nullcontext()
    if event.get("provider") == "codex":
        return module._binding_lock_for_opaque(event["session_id"])
    return _OPENCODE_LOCK


def _receipt_valid(module, binding, event):
    """Allow claimed WAL recovery, but only for the same exact receipt."""
    completion = event["completion_id"]
    known = (completion in binding.get("attempted_completion_ids", [])
             or completion in binding.get("delivered_completion_ids", []))
    if not known:
        # Keep the existing provider validator authoritative for fresh admission.
        return module._completion_is_current(event, binding)
    if event["provider"] == "codex":
        if binding.get("opaque_target_session_id") != event["session_id"]:
            return False
    elif binding.get("opaque_session_id") != event["session_id"]:
        return False
    completed = module._parse_time(event.get("completed_at"))
    bound = module._parse_time(binding.get("bound_at"))
    if bound is None or completed is None or completed < bound:
        return False
    completion = event["completion_id"]
    known = (completion in binding.get("attempted_completion_ids", [])
             or completion in binding.get("delivered_completion_ids", []))
    if known:
        if event["provider"] == "codex":
            # A consumed binding must never accept a different receipt/turn.
            if binding.get("return_state") not in {"prepared", "armed", "consumed"}:
                return False
            return any(anchor.get("completion_id") == completion
                       and anchor.get("cursor") == event["cursor"]
                       and anchor.get("phase") == event["phase"]
                       for anchor in binding.get("receipt_anchors", []))
        return True
    return module._completion_is_current(event, binding)


def lookup_event(event: dict) -> dict | None:
    if not _terminal(event):
        return None
    module = _module(event["provider"])
    with _lock(module, event):
        binding = module._read_binding(event["session_id"])
        if binding is None or not _receipt_valid(module, binding, event):
            return None
        # This is fresh admission, not WAL recovery. The dispatcher recovers
        # persisted jobs through validate/claim/complete directly. Re-admitting
        # a known receipt after journal retention would start the same return
        # again; an attempted receipt with no WAL is ambiguous, not fresh work.
        if (event["completion_id"] in binding.get("attempted_completion_ids", [])
                or event["completion_id"] in binding.get("delivered_completion_ids", [])):
            return None
        codex = event["provider"] == "codex"
        source = binding["source_codex_thread_id"] if codex else binding["codex_thread_id"]
        return dict(provider=event["provider"], source_task_id=source,
                    completion_id=event["completion_id"],
                    binding_generation=binding["binding_id"], cursor=event["cursor"],
                    terminal_phase=event["phase"], workspace=binding.get("workspace", ""),
                    session_id=event["session_id"],
                    opencode_session_id=binding.get("opencode_session_id", "") if not codex else "",
                    codex_thread_id=binding["target_codex_thread_id"] if codex else source,
                    agent_id=event.get("agent_id", ""), completed_at=event.get("completed_at", ""))


def _event(job):
    return dict(provider=job.provider, cursor=job.cursor, session_id=job.session_id,
                completion_id=job.completion_id, phase=job.terminal_phase,
                agent_id=getattr(job, "agent_id", ""),
                completed_at=getattr(job, "completed_at", ""))


def _binding(job, event):
    if not _terminal(event):
        return None
    module = _module(job.provider)
    binding = module._read_binding(job.session_id)
    if binding is None or binding.get("binding_id") != job.binding_generation:
        return None
    if not isinstance(binding.get("workspace"), str):
        return None
    codex = job.provider == "codex"
    source = binding["source_codex_thread_id"] if codex else binding["codex_thread_id"]
    target = binding["target_codex_thread_id"] if codex else source
    if source != job.source_task_id or target != job.codex_thread_id:
        return None
    if Path(binding["workspace"]).resolve() != Path(job.workspace).resolve():
        return None
    return binding if _receipt_valid(module, binding, event) else None


def validate(job) -> bool:
    event = _event(job)
    if not _terminal(event):
        return False
    with _lock(_module(job.provider), event):
        return _binding(job, event) is not None


def claim(job) -> bool:
    event = _event(job)
    if not _terminal(event):
        return False
    module = _module(job.provider)
    with _lock(module, event):
        binding = _binding(job, event)
        if binding is None:
            return False
        if (job.completion_id in binding["attempted_completion_ids"]
                or job.completion_id in binding["delivered_completion_ids"]):
            return True
        if job.provider == "codex":
            claimed = module._claim_completion(event, job.binding_generation)
        else:
            claimed = module._claim_completion(job.session_id, job.binding_generation,
                                               job.cursor, job.completion_id)
        return claimed is not None and _binding(job, event) is not None


def complete(job) -> bool:
    event = _event(job)
    if not _terminal(event):
        return False
    module = _module(job.provider)
    with _lock(module, event):
        binding = _binding(job, event)
        if binding is None:
            return False
        if job.completion_id in binding["delivered_completion_ids"]:
            return True
        if job.completion_id not in binding["attempted_completion_ids"]:
            return False
        if job.provider == "codex":
            module._complete_completion(event, job.binding_generation)
        else:
            module._complete_completion(job.session_id, job.binding_generation,
                                       job.cursor, job.completion_id)
        binding = _binding(job, event)
        return binding is not None and job.completion_id in binding["delivered_completion_ids"]


def prompt(job) -> str:
    event = _event(job)
    if not _terminal(event):
        raise ValueError("invalid provider receipt")
    module = _module(job.provider)
    with _lock(module, event):
        binding = _binding(job, event)
        if binding is None:
            raise ValueError("provider receipt binding changed")
        if job.provider == "codex":
            return module._resume_prompt(binding, event)
        result = module._resume_prompt(binding, job.terminal_phase, job.completion_id)
        if job.completion_id not in result:
            result += f" Terminal receipt: {job.completion_id}; phase: {job.terminal_phase}."
        return result


def source_matches_event(source_id: str, event: dict) -> bool:
    """Match only a validated terminal for this known source, never scan bindings."""
    if not _terminal(event) or event["provider"] != "codex":
        return False
    module = _module("codex")
    if module._parse_time(event.get("completed_at")) is None:
        return False
    if not isinstance(source_id, str) or module.THREAD_ID_PATTERN.fullmatch(source_id) is None:
        return False
    return event["session_id"] == module._opaque_thread_id(source_id.lower())
