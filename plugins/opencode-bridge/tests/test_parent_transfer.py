from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


codex = _load("codex_relay", "codex_relay.py")
relay = _load("opencode_relay", "opencode_relay.py")
bridge = _load("opencode_bridge", "opencode_bridge.py")


TARGET_THREAD_ID = "01a07246-023e-7fe1-91ef-a0ca77a4345d"
OLD_SOURCE_THREAD_ID = "01a0728f-f260-71f2-9115-8618290837c1"
NEW_SOURCE_THREAD_ID = "01a071f6-03f8-7611-b959-dbd23c7cd631"
OTHER_TARGET = "019fd3ed-cb16-7362-af22-cf341aa706d5"
BASELINE_ANCHOR = {
    "turn_id": "019fecdb-6d14-7972-a192-a56c38193837",
    "item_hashes": {"item-before-send": "a" * 64},
}
CURRENT_ANCHOR = {
    "turn_id": "019fecdb-6d14-7972-a192-a56c38193838",
    "item_hashes": {"item-after-send": "b" * 64},
}


def completion(binding, cursor, *, phase="completed"):
    return {
        "cursor": cursor,
        "completion_id": f"completion:{cursor:064x}",
        "provider": "codex",
        "session_id": binding["opaque_target_session_id"],
        "agent_id": f"turn:{cursor:064x}",
        "phase": phase,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


def _binding_file_bytes(app_data: str, target_id: str) -> bytes:
    opaque = codex._opaque_thread_id(target_id)
    path = codex._binding_path_for_opaque(opaque)
    with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
        return path.read_bytes()


# --- Module-level functions for multiprocessing (Windows spawn requires picklable targets) ---

def _child_transfer(app_data_val, ws_val, target_id, old_bid, new_source, result_path):
    import importlib.util as _iu, json as _j, os, sys as _sys
    _sys.path.insert(0, str(SCRIPTS))
    spec = _iu.spec_from_file_location("codex_relay", SCRIPTS / "codex_relay.py")
    mod = _iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    os.environ["LOCALAPPDATA"] = app_data_val
    try:
        r = mod.transfer_return_parent(
            target_id, old_bid, new_source, ws_val, transfer_parent=True,
        )
        Path(result_path).write_text(
            _j.dumps({"ok": True, "changed": r["binding_changed"]}), encoding="utf-8",
        )
    except Exception as e:
        Path(result_path).write_text(
            _j.dumps({"ok": False, "error": str(e)}), encoding="utf-8",
        )


def _child_lock_hold(app_data_val, ws_val, target_id, signal_path):
    import importlib.util as _iu, os, sys as _sys, time
    _sys.path.insert(0, str(SCRIPTS))
    spec = _iu.spec_from_file_location("codex_relay", SCRIPTS / "codex_relay.py")
    mod = _iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    os.environ["LOCALAPPDATA"] = app_data_val
    lock = mod._binding_lock_for_opaque(mod._opaque_thread_id(target_id))
    acquired = lock.acquire(blocking=True, timeout=10)
    if acquired:
        Path(signal_path).write_text("locked", encoding="utf-8")
    time.sleep(0.5)
    if acquired:
        lock.release()
    Path(signal_path).write_text("done", encoding="utf-8")


def _child_lock_exit(app_data_val, target_id):
    import importlib.util as _iu, os, sys as _sys, time
    _sys.path.insert(0, str(SCRIPTS))
    spec = _iu.spec_from_file_location("codex_relay", SCRIPTS / "codex_relay.py")
    mod = _iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    os.environ["LOCALAPPDATA"] = app_data_val
    lock = mod._binding_lock_for_opaque(mod._opaque_thread_id(target_id))
    acquired = lock.acquire(blocking=True, timeout=10)
    if not acquired:
        return
    time.sleep(0.3)
    lock.release()


class ParentTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.anchor_patch = mock.patch.object(
            codex, "capture_thread_anchor", return_value=CURRENT_ANCHOR,
        )
        self.anchor_patch.start()

    def tearDown(self) -> None:
        self.anchor_patch.stop()

    def _register(self, workspace, *, source=OLD_SOURCE_THREAD_ID, after_cursor=0):
        return codex.register_binding(
            TARGET_THREAD_ID, source, workspace,
            "target-cursor-before-send", after_cursor, BASELINE_ANCHOR,
        )

    def test_get_return_parent_inspects_binding(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as ws:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _, _ = self._register(ws)
                result = codex.get_return_parent(TARGET_THREAD_ID)
        self.assertEqual(result["state"], "bound")
        self.assertEqual(result["source_thread_id"], OLD_SOURCE_THREAD_ID)
        self.assertEqual(result["workspace"], str(Path(ws).resolve()))
        self.assertEqual(result["binding_id"], binding["binding_id"])

    def test_get_return_parent_unbound_target(self) -> None:
        with tempfile.TemporaryDirectory() as app_data:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                result = codex.get_return_parent(OTHER_TARGET)
        self.assertEqual(result["state"], "unbound")

    def test_transfer_old_parent_to_new_parent(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as ws:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                old_binding, _, _ = self._register(ws, after_cursor=5)
                result = codex.transfer_return_parent(
                    TARGET_THREAD_ID, old_binding["binding_id"],
                    NEW_SOURCE_THREAD_ID, ws, transfer_parent=True,
                )
                persisted = codex.binding_status(TARGET_THREAD_ID)
        self.assertTrue(result["binding_changed"])
        self.assertEqual(result["previous_source_thread_id"], OLD_SOURCE_THREAD_ID)
        self.assertEqual(result["new_source_thread_id"], NEW_SOURCE_THREAD_ID)
        self.assertEqual(result["return_state"], "parent_only")
        self.assertFalse(result["return_armed"])
        self.assertEqual(persisted["return_state"], "parent_only")
        self.assertEqual(persisted["source_codex_thread_id"], NEW_SOURCE_THREAD_ID)
        self.assertNotEqual(persisted["binding_id"], old_binding["binding_id"])

    def test_transfer_preserves_delivery_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as ws:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                old_binding, _, _ = self._register(ws, after_cursor=5)
                record = completion(old_binding, 6)
                claimed = codex._claim_completion(record, old_binding["binding_id"])
                self.assertIsNotNone(claimed)
                result = codex.transfer_return_parent(
                    TARGET_THREAD_ID, old_binding["binding_id"],
                    NEW_SOURCE_THREAD_ID, ws, transfer_parent=True,
                )
                persisted = codex.binding_status(TARGET_THREAD_ID)
        self.assertTrue(result["already_started"])
        self.assertIn(record["completion_id"], persisted["attempted_completion_ids"])

    def test_no_return_emitted_on_completion_while_parent_only(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as ws:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                old_binding, _, _ = self._register(ws)
                codex.transfer_return_parent(
                    TARGET_THREAD_ID, old_binding["binding_id"],
                    NEW_SOURCE_THREAD_ID, ws, transfer_parent=True,
                )
                current = codex.binding_status(TARGET_THREAD_ID)
                result = codex.process_completion(
                    completion(current, 1),
                    runner=lambda cmd, cwd: self.fail("parent_only should not wake"),
                    command_prefix=["codex"],
                )
        self.assertEqual(result, "stale")

    def test_subsequent_bind_send_confirm_works_from_parent_only(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as ws:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                old_binding, _, _ = self._register(ws)
                codex.transfer_return_parent(
                    TARGET_THREAD_ID, old_binding["binding_id"],
                    NEW_SOURCE_THREAD_ID, ws, transfer_parent=True,
                )
                new_binding, changed, new_token = codex.register_binding(
                    TARGET_THREAD_ID, NEW_SOURCE_THREAD_ID, ws,
                    "fresh-cursor-after-transfer", 10, CURRENT_ANCHOR,
                )
                self.assertTrue(changed)
                self.assertEqual(new_binding["return_state"], "prepared")
                self.assertTrue(
                    codex.confirm_binding(TARGET_THREAD_ID, new_binding["binding_id"], new_token)
                )
                confirmed = codex.binding_status(TARGET_THREAD_ID)
                wakes = []
                result = codex.process_completion(
                    completion(new_binding, 11),
                    runner=lambda cmd, cwd: wakes.append(cmd) or 0,
                    command_prefix=["codex"],
                )
        self.assertEqual(confirmed["return_state"], "armed")
        self.assertEqual(result, "resumed")
        self.assertEqual(len(wakes), 1)

    def test_stale_cas_cannot_overwrite_newer_parent(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as ws:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                old_binding, _, _ = self._register(ws)
                codex.transfer_return_parent(
                    TARGET_THREAD_ID, old_binding["binding_id"],
                    NEW_SOURCE_THREAD_ID, ws, transfer_parent=True,
                )
                with self.assertRaisesRegex(ValueError, "stale"):
                    codex.transfer_return_parent(
                        TARGET_THREAD_ID, old_binding["binding_id"],
                        OLD_SOURCE_THREAD_ID, ws, transfer_parent=True,
                    )

    def test_same_parent_retry_preserves_armed_route(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as ws:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                old_binding, _, _ = self._register(ws)
                token = self._get_token(old_binding)
                self.assertTrue(
                    codex.confirm_binding(TARGET_THREAD_ID, old_binding["binding_id"], token)
                )
                armed = codex.binding_status(TARGET_THREAD_ID)
                self.assertEqual(armed["return_state"], "armed")
                result = codex.transfer_return_parent(
                    TARGET_THREAD_ID, old_binding["binding_id"],
                    OLD_SOURCE_THREAD_ID, ws, transfer_parent=True,
                )
                after = codex.binding_status(TARGET_THREAD_ID)
        self.assertFalse(result["binding_changed"])
        self.assertEqual(result["return_state"], "armed")
        self.assertTrue(result["return_armed"])
        self.assertEqual(after["binding_id"], old_binding["binding_id"])
        self.assertEqual(after["return_state"], "armed")

    def test_same_parent_workspace_change_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as ws:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                old_binding, _, _ = self._register(ws)
                other = tempfile.mkdtemp()
                try:
                    with self.assertRaisesRegex(ValueError, "same-parent workspace change"):
                        codex.transfer_return_parent(
                            TARGET_THREAD_ID, old_binding["binding_id"],
                            OLD_SOURCE_THREAD_ID, other, transfer_parent=True,
                        )
                finally:
                    os.rmdir(other)

    def test_self_target_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as ws:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _, _ = self._register(ws)
                with self.assertRaisesRegex(ValueError, "must be different"):
                    codex.transfer_return_parent(
                        TARGET_THREAD_ID, binding["binding_id"],
                        TARGET_THREAD_ID, ws, transfer_parent=True,
                    )

    def test_missing_transfer_flag_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as ws:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _, _ = self._register(ws)
                with self.assertRaisesRegex(ValueError, "transfer_parent=true"):
                    codex.transfer_return_parent(
                        TARGET_THREAD_ID, binding["binding_id"],
                        NEW_SOURCE_THREAD_ID, ws, transfer_parent=False,
                    )

    def test_invalid_path_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as ws:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _, _ = self._register(ws)
                with self.assertRaises((ValueError, OSError)):
                    codex.transfer_return_parent(
                        TARGET_THREAD_ID, binding["binding_id"],
                        NEW_SOURCE_THREAD_ID, "C:/nonexistent/path/xyz",
                        transfer_parent=True,
                    )

    def test_malformed_identifier_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as ws:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _, _ = self._register(ws)
                with self.assertRaises(ValueError):
                    codex.transfer_return_parent(
                        TARGET_THREAD_ID, "not-a-valid-binding-id",
                        NEW_SOURCE_THREAD_ID, ws, transfer_parent=True,
                    )

    def test_queued_old_generation_receipt_cannot_run(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as ws:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                old_binding, _, _ = self._register(ws)
                old_comp = completion(old_binding, 1)
                codex.transfer_return_parent(
                    TARGET_THREAD_ID, old_binding["binding_id"],
                    NEW_SOURCE_THREAD_ID, ws, transfer_parent=True,
                )
                current = codex.binding_status(TARGET_THREAD_ID)
                result = codex.process_completion(
                    {**old_comp, "session_id": current["opaque_target_session_id"]},
                    runner=lambda cmd, cwd: self.fail("old generation receipt should not run"),
                    command_prefix=["codex"],
                )
        self.assertEqual(result, "stale")

    def test_all_invalid_requests_leave_state_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as ws:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                old_binding, _, _ = self._register(ws)
                before_bytes = _binding_file_bytes(app_data, TARGET_THREAD_ID)
                errors = []
                try:
                    codex.transfer_return_parent(
                        TARGET_THREAD_ID, "not-valid",
                        NEW_SOURCE_THREAD_ID, ws, transfer_parent=True,
                    )
                except ValueError:
                    errors.append("stale_id")
                try:
                    codex.transfer_return_parent(
                        TARGET_THREAD_ID, old_binding["binding_id"],
                        TARGET_THREAD_ID, ws, transfer_parent=True,
                    )
                except ValueError:
                    errors.append("self_target")
                try:
                    codex.transfer_return_parent(
                        TARGET_THREAD_ID, old_binding["binding_id"],
                        NEW_SOURCE_THREAD_ID, ws, transfer_parent=False,
                    )
                except ValueError:
                    errors.append("missing_flag")
                after_bytes = _binding_file_bytes(app_data, TARGET_THREAD_ID)
        self.assertEqual(len(errors), 3)
        self.assertEqual(before_bytes, after_bytes)

    def test_injected_atomic_write_failure_preserves_prior(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as ws:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                old_binding, _, _ = self._register(ws)
                original_write = codex._atomic_json_write

                def failing_write(path, payload):
                    if str(path).endswith(".json") and "codex-bindings" in str(path):
                        raise OSError("injected write failure")
                    return original_write(path, payload)

                with mock.patch.object(codex, "_atomic_json_write", side_effect=failing_write):
                    with self.assertRaises(OSError):
                        codex.transfer_return_parent(
                            TARGET_THREAD_ID, old_binding["binding_id"],
                            NEW_SOURCE_THREAD_ID, ws, transfer_parent=True,
                        )
                preserved = codex.binding_status(TARGET_THREAD_ID)
        self.assertEqual(preserved["binding_id"], old_binding["binding_id"])
        self.assertEqual(preserved["source_codex_thread_id"], OLD_SOURCE_THREAD_ID)

    def _get_token(self, binding):
        rollback = codex._read_rollback(codex._opaque_thread_id(TARGET_THREAD_ID))
        return rollback["rollback_token"] if rollback else None


class McpCallToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.anchor_patch = mock.patch.object(
            codex, "capture_thread_anchor", return_value=CURRENT_ANCHOR,
        )
        self.anchor_patch.start()

    def tearDown(self) -> None:
        self.anchor_patch.stop()

    def _rpc(self, name, arguments):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for owner, attribute in [
                (bridge, "ensure_server"), (bridge, "_request"),
                (bridge, "capture_thread_anchor"), (bridge, "read_thread_delta"),
                (bridge, "_petcrew_inbox"), (bridge.urllib.request, "urlopen"),
                (codex, "capture_thread_anchor"),
            ]:
                stack.enter_context(mock.patch.object(
                    owner, attribute, side_effect=AssertionError("parent metadata must stay local")
                ))
            return bridge.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": name, "arguments": arguments}})

    def test_get_return_parent_via_rpc_no_workspace_required(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as ws:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                codex.register_binding(
                    TARGET_THREAD_ID, OLD_SOURCE_THREAD_ID, ws,
                    "cursor", 0, BASELINE_ANCHOR,
                )
                resp = self._rpc("codex_get_return_parent", {
                    "target_thread_id": TARGET_THREAD_ID,
                })
        content = resp["result"]["structuredContent"]
        self.assertEqual(content["state"], "bound")
        self.assertEqual(content["source_thread_id"], OLD_SOURCE_THREAD_ID)

    def test_get_return_parent_unbound_via_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as app_data:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                resp = self._rpc("codex_get_return_parent", {
                    "target_thread_id": OTHER_TARGET,
                })
        content = resp["result"]["structuredContent"]
        self.assertEqual(content["state"], "unbound")

    def test_transfer_via_rpc_no_reader_inbox_network(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as ws:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                old_binding, _, _ = codex.register_binding(
                    TARGET_THREAD_ID, OLD_SOURCE_THREAD_ID, ws,
                    "cursor", 0, BASELINE_ANCHOR,
                )
                with mock.patch.object(bridge, "capture_thread_anchor", side_effect=RuntimeError("no network")):
                    with mock.patch.object(bridge, "_petcrew_inbox", side_effect=RuntimeError("no inbox")):
                        with mock.patch.object(bridge, "read_thread_delta", side_effect=RuntimeError("no reader")):
                            resp = self._rpc("codex_transfer_return_parent", {
                                "target_thread_id": TARGET_THREAD_ID,
                                "expected_binding_id": old_binding["binding_id"],
                                "new_source_thread_id": NEW_SOURCE_THREAD_ID,
                                "new_workspace": ws,
                                "transfer_parent": True,
                            })
        content = resp["result"]["structuredContent"]
        self.assertTrue(content["binding_changed"])
        self.assertEqual(content["return_state"], "parent_only")

    def test_get_parent_must_not_hit_server_startup(self) -> None:
        with tempfile.TemporaryDirectory() as app_data:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                with mock.patch.object(bridge, "ensure_server", side_effect=RuntimeError("must not start")):
                    resp = self._rpc("codex_get_return_parent", {
                        "target_thread_id": OTHER_TARGET,
                    })
        content = resp["result"]["structuredContent"]
        self.assertEqual(content["state"], "unbound")

    def test_transfer_missing_flag_via_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as ws:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                old_binding, _, _ = codex.register_binding(
                    TARGET_THREAD_ID, OLD_SOURCE_THREAD_ID, ws,
                    "cursor", 0, BASELINE_ANCHOR,
                )
                resp = self._rpc("codex_transfer_return_parent", {
                    "target_thread_id": TARGET_THREAD_ID,
                    "expected_binding_id": old_binding["binding_id"],
                    "new_source_thread_id": NEW_SOURCE_THREAD_ID,
                    "new_workspace": ws,
                    "transfer_parent": False,
                })
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("transfer_parent=true", resp["result"]["structuredContent"]["error"])


class ProcessSafeLockTests(unittest.TestCase):
    def test_os_lock_reentrancy(self) -> None:
        with tempfile.TemporaryDirectory() as app_data:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                lock = codex._binding_lock_for_opaque(
                    codex._opaque_thread_id(TARGET_THREAD_ID)
                )
                with lock:
                    with lock:
                        pass
                self.assertEqual(lock._count, 0)

    def test_os_lock_release_on_exception(self) -> None:
        with tempfile.TemporaryDirectory() as app_data:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                lock = codex._binding_lock_for_opaque(
                    codex._opaque_thread_id(TARGET_THREAD_ID)
                )
                try:
                    with lock:
                        raise ValueError("test")
                except ValueError:
                    pass
                self.assertEqual(lock._count, 0)
                self.assertTrue(lock.acquire(blocking=False))
                lock.release()

    def test_two_processes_one_cas_winner(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as ws:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                old_binding, _, _ = codex.register_binding(
                    TARGET_THREAD_ID, OLD_SOURCE_THREAD_ID, ws,
                    "cursor", 0, BASELINE_ANCHOR,
                )
                opaque = codex._opaque_thread_id(TARGET_THREAD_ID)

                signal = tempfile.mktemp(suffix=".signal")
                p = multiprocessing.Process(
                    target=_child_lock_hold,
                    args=(app_data, ws, TARGET_THREAD_ID, signal),
                )
                p.start()
                for _ in range(50):
                    time.sleep(0.1)
                    if Path(signal).exists() and Path(signal).read_text(encoding="utf-8") == "locked":
                        break
                lock = codex._binding_lock_for_opaque(opaque)
                b = lock.acquire(blocking=False)
                self.assertFalse(b, "child holds the lock; nonblocking acquire must fail")
                p.join(5)
                self.assertEqual(p.exitcode, 0)
                a = lock.acquire(blocking=True, timeout=2)
                self.assertTrue(a, "lock released after child exit")
                lock.release()
                try:
                    os.unlink(signal)
                except OSError:
                    pass

    def test_subprocess_register_vs_transfer_contention(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as ws:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                old_binding, _, _ = codex.register_binding(
                    TARGET_THREAD_ID, OLD_SOURCE_THREAD_ID, ws,
                    "cursor", 0, BASELINE_ANCHOR,
                )
                bid = old_binding["binding_id"]
                result_a = tempfile.mktemp(suffix=".a")
                result_b = tempfile.mktemp(suffix=".b")
                pa = multiprocessing.Process(
                    target=_child_transfer,
                    args=(app_data, ws, TARGET_THREAD_ID, bid,
                          NEW_SOURCE_THREAD_ID, result_a),
                )
                pb = multiprocessing.Process(
                    target=_child_transfer,
                    args=(app_data, ws, TARGET_THREAD_ID, bid,
                          NEW_SOURCE_THREAD_ID, result_b),
                )
                pa.start(); pb.start()
                pa.join(10); pb.join(10)
                self.assertEqual(pa.exitcode, 0)
                self.assertEqual(pb.exitcode, 0)
                ra = json.loads(Path(result_a).read_text(encoding="utf-8"))
                rb = json.loads(Path(result_b).read_text(encoding="utf-8"))
                winners = [r for r in (ra, rb) if r.get("ok") and r.get("changed")]
                losers = [r for r in (ra, rb) if not r.get("ok")]
                self.assertEqual(len(winners), 1, f"exactly one winner, got: {winners}")
                self.assertEqual(len(losers), 1, f"exactly one loser, got: {losers}")
                self.assertIn("stale", losers[0]["error"])
                for p in (result_a, result_b):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass

    def test_release_after_process_exit(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as ws:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                old_binding, _, _ = codex.register_binding(
                    TARGET_THREAD_ID, OLD_SOURCE_THREAD_ID, ws,
                    "cursor", 0, BASELINE_ANCHOR,
                )
                opaque = codex._opaque_thread_id(TARGET_THREAD_ID)
                signal = tempfile.mktemp(suffix=".signal")
                p = multiprocessing.Process(
                    target=_child_lock_hold,
                    args=(app_data, ws, TARGET_THREAD_ID, signal),
                )
                p.start()
                for _ in range(50):
                    time.sleep(0.1)
                    if Path(signal).exists() and Path(signal).read_text(encoding="utf-8") == "locked":
                        break
                lock = codex._binding_lock_for_opaque(opaque)
                self.assertFalse(lock.acquire(blocking=False))
                p.join(5)
                self.assertEqual(p.exitcode, 0)
                self.assertTrue(lock.acquire(blocking=True, timeout=2))
                lock.release()
                try:
                    os.unlink(signal)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
