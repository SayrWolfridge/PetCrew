from __future__ import annotations

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import os
import sys
import tempfile
import threading
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


TARGET_THREAD_ID = "019fd3ed-cb16-7362-af22-cf341aa706d5"
SOURCE_THREAD_ID = "019fd933-87b0-7d93-9666-bf02bf4f03d5"
OTHER_SOURCE_THREAD_ID = "019fe6a7-09e3-75f0-a368-4eb18afd746e"
BASELINE_TURN_ID = "019fecdb-6d14-7972-a192-a56c38193837"
BASELINE_ANCHOR = {
    "turn_id": BASELINE_TURN_ID,
    "item_hashes": {"item-before-send": "a" * 64},
}
CURRENT_ANCHOR = {
    "turn_id": "019fecdb-6d14-7972-a192-a56c38193838",
    "item_hashes": {"item-after-send": "b" * 64},
}


def completion(
    binding,
    cursor,
    *,
    completed_at=None,
    completion_id=None,
    phase="completed",
):
    return {
        "cursor": cursor,
        "completion_id": completion_id or f"completion:{cursor:064x}",
        "provider": "codex",
        "session_id": binding["opaque_target_session_id"],
        "agent_id": f"turn:{cursor:064x}",
        "phase": phase,
        "completed_at": completed_at or datetime.now(timezone.utc).isoformat(),
    }


class CodexRelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.anchor_patch = mock.patch.object(
            codex,
            "capture_thread_anchor",
            return_value=CURRENT_ANCHOR,
        )
        self.capture_anchor = self.anchor_patch.start()

    def tearDown(self) -> None:
        self.anchor_patch.stop()

    def _register(self, workspace, *, after_cursor=0, source=SOURCE_THREAD_ID):
        return codex.register_binding(
            TARGET_THREAD_ID,
            source,
            workspace,
            "target-cursor-before-send",
            after_cursor,
            BASELINE_ANCHOR,
        )

    def test_registration_requires_two_tasks_and_uses_opaque_filename(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, changed, token = self._register(workspace, after_cursor=7)
                files = list(codex.binding_dir().glob("*.json"))
                with self.assertRaisesRegex(ValueError, "must be different"):
                    codex.register_binding(
                        TARGET_THREAD_ID,
                        TARGET_THREAD_ID,
                        workspace,
                        "cursor",
                        7,
                        BASELINE_ANCHOR,
                    )
        self.assertTrue(changed)
        self.assertRegex(token, r"^[0-9a-f]{64}$")
        self.assertEqual(binding["after_cursor"], 7)
        self.assertEqual(binding["baseline_anchor"], BASELINE_ANCHOR)
        self.assertEqual(binding["return_state"], "prepared")
        self.assertEqual(len(files), 1)
        self.assertNotIn(TARGET_THREAD_ID, files[0].name)

    def test_completion_resumes_exact_source_with_fresh_read_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _changed, _token = self._register(workspace, after_cursor=3)
                captured = []
                result = codex.process_completion(
                    completion(binding, 4),
                    runner=lambda command, cwd: captured.append((command, cwd)) or 0,
                    command_prefix=["codex"],
                )
                persisted = codex.binding_status(TARGET_THREAD_ID)
        self.assertEqual(result, "resumed")
        self.assertEqual(captured[0][0][0:4], ["codex", "exec", "resume", SOURCE_THREAD_ID])
        self.assertIn(TARGET_THREAD_ID, captured[0][0][4])
        self.assertIn("codex_read_return", captured[0][0][4])
        self.assertNotIn("codex_app__wait_threads", captured[0][0][4])
        self.assertNotIn("latest assistant", captured[0][0][4].lower())
        self.assertEqual(captured[0][1], str(Path(workspace).resolve()))
        self.assertEqual(persisted["after_cursor"], 4)
        self.assertEqual(persisted["baseline_anchor"], CURRENT_ANCHOR)
        self.assertEqual(persisted["return_state"], "consumed")
        self.assertFalse(persisted["pending_confirmation"])

    def test_terminal_receipt_is_attempted_at_most_once(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _changed, _token = self._register(workspace)
                record = completion(binding, 1)
                self.assertEqual(
                    codex.process_completion(
                        record,
                        runner=lambda command, cwd: 0,
                        command_prefix=["codex"],
                    ),
                    "resumed",
                )
                self.assertEqual(
                    codex.process_completion(
                        {**record, "cursor": 2},
                        runner=lambda command, cwd: self.fail("duplicate wake"),
                        command_prefix=["codex"],
                    ),
                    "stale",
                )

    def test_one_dispatch_wakes_once_and_explicit_follow_up_rearms_once(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                first, _changed, first_token = self._register(workspace)
                self.assertTrue(
                    codex.confirm_binding(
                        TARGET_THREAD_ID,
                        first["binding_id"],
                        first_token,
                    )
                )
                wakes = []
                self.assertEqual(
                    codex.process_completion(
                        completion(first, 1),
                        runner=lambda command, cwd: wakes.append((command, cwd)) or 0,
                        command_prefix=["codex"],
                    ),
                    "resumed",
                )
                self.assertEqual(
                    codex.process_completion(
                        completion(first, 2),
                        runner=lambda command, cwd: self.fail("manual turn woke source"),
                        command_prefix=["codex"],
                    ),
                    "stale",
                )
                second, changed, second_token = codex.register_binding(
                    TARGET_THREAD_ID,
                    SOURCE_THREAD_ID,
                    workspace,
                    "target-cursor-before-follow-up",
                    2,
                    CURRENT_ANCHOR,
                )
                self.assertTrue(changed)
                self.assertNotEqual(second["binding_id"], first["binding_id"])
                self.assertTrue(
                    codex.confirm_binding(
                        TARGET_THREAD_ID,
                        second["binding_id"],
                        second_token,
                    )
                )
                self.assertEqual(
                    codex.process_completion(
                        completion(second, 3),
                        runner=lambda command, cwd: wakes.append((command, cwd)) or 0,
                        command_prefix=["codex"],
                    ),
                    "resumed",
                )
                self.assertEqual(
                    codex.process_completion(
                        completion(second, 4),
                        runner=lambda command, cwd: self.fail("second manual turn woke source"),
                        command_prefix=["codex"],
                    ),
                    "stale",
                )
                status = codex.binding_status(TARGET_THREAD_ID)
        self.assertEqual(len(wakes), 2)
        self.assertEqual(status["return_state"], "consumed")

    def test_legacy_binding_is_fail_closed_as_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _changed, _token = self._register(workspace)
                legacy = dict(binding)
                legacy["schema_version"] = 2
                legacy.pop("return_state")
                codex._atomic_json_write(codex._binding_path(TARGET_THREAD_ID), legacy)
                status = codex.binding_status(TARGET_THREAD_ID)
                result = codex.process_completion(
                    completion(binding, 1),
                    runner=lambda command, cwd: self.fail("legacy binding woke source"),
                    command_prefix=["codex"],
                )
        self.assertEqual(status["schema_version"], 3)
        self.assertEqual(status["return_state"], "consumed")
        self.assertEqual(result, "stale")

    def test_failed_resume_keeps_exact_receipt_and_does_not_claim_later_turn(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _changed, _token = self._register(workspace)
                record = completion(binding, 5)
                self.assertEqual(
                    codex.process_completion(
                        record,
                        runner=lambda command, cwd: 1,
                        command_prefix=["codex"],
                    ),
                    "deferred",
                )
                self.assertEqual(
                    codex.process_completion(
                        record,
                        runner=lambda command, cwd: self.fail("failed receipt retried"),
                        command_prefix=["codex"],
                    ),
                    "stale",
                )
                self.assertEqual(
                    codex.process_completion(
                        completion(binding, 6),
                        runner=lambda command, cwd: self.fail("later turn woke source"),
                        command_prefix=["codex"],
                    ),
                    "stale",
                )

    def test_busy_source_gets_one_event_driven_retry_after_its_turn_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _changed, _token = self._register(workspace)
                record = completion(binding, 8)
                self.assertEqual(
                    codex.process_completion(
                        record,
                        runner=lambda command, cwd: 1,
                        command_prefix=["codex"],
                    ),
                    "deferred",
                )
                captured = []
                source_record = {
                    "provider": "codex",
                    "session_id": codex._opaque_thread_id(SOURCE_THREAD_ID),
                    "phase": "completed",
                }
                self.assertEqual(
                    codex.retry_pending_for_source(
                        source_record,
                        runner=lambda command, cwd: captured.append((command, cwd)) or 0,
                        command_prefix=["codex"],
                    ),
                    "resumed",
                )
                persisted = codex.binding_status(TARGET_THREAD_ID)
                self.assertEqual(codex.retry_pending_for_source(source_record), "none")
        self.assertEqual(captured[0][0][0:4], ["codex", "exec", "resume", SOURCE_THREAD_ID])
        self.assertIn(TARGET_THREAD_ID, captured[0][0][4])
        self.assertIn(record["completion_id"], captured[0][0][4])
        self.assertIn(record["completion_id"], persisted["delivered_completion_ids"])

    def test_pre_binding_completion_and_baseline_cursor_are_stale(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _changed, _token = self._register(workspace, after_cursor=10)
                old_time = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
                self.assertEqual(
                    codex.process_completion(
                        completion(binding, 11, completed_at=old_time),
                        runner=lambda command, cwd: self.fail("old completion resumed"),
                        command_prefix=["codex"],
                    ),
                    "stale",
                )
                self.assertEqual(
                    codex.process_completion(
                        completion(binding, 10),
                        runner=lambda command, cwd: self.fail("baseline completion resumed"),
                        command_prefix=["codex"],
                    ),
                    "stale",
                )

    def test_transfer_requires_approval_and_failed_send_can_restore_parent(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                original, _changed, original_token = self._register(workspace)
                self.assertTrue(
                    codex.confirm_binding(
                        TARGET_THREAD_ID,
                        original["binding_id"],
                        original_token,
                    )
                )
                with self.assertRaisesRegex(ValueError, "transfer_parent=true"):
                    self._register(workspace, source=OTHER_SOURCE_THREAD_ID)
                transferred, changed, rollback_token = codex.register_binding(
                    TARGET_THREAD_ID,
                    OTHER_SOURCE_THREAD_ID,
                    workspace,
                    "cursor-before-transfer",
                    12,
                    BASELINE_ANCHOR,
                    transfer_parent=True,
                )
                self.assertTrue(changed)
                self.assertTrue(
                    codex.rollback_binding(
                        TARGET_THREAD_ID,
                        transferred["binding_id"],
                        rollback_token,
                    )
                )
                restored = codex.binding_status(TARGET_THREAD_ID)
        self.assertEqual(restored["binding_id"], original["binding_id"])
        self.assertEqual(restored["source_codex_thread_id"], SOURCE_THREAD_ID)

    def test_confirm_and_detach_require_exact_binding_owner(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _changed, token = self._register(workspace)
                self.assertTrue(
                    codex.confirm_binding(TARGET_THREAD_ID, binding["binding_id"], token)
                )
                confirmed = codex.binding_status(TARGET_THREAD_ID)
                self.assertFalse(confirmed["pending_confirmation"])
                self.assertEqual(confirmed["return_state"], "armed")
                with self.assertRaisesRegex(ValueError, "another source task"):
                    codex.detach_binding(TARGET_THREAD_ID, OTHER_SOURCE_THREAD_ID)
                self.assertTrue(codex.detach_binding(TARGET_THREAD_ID, SOURCE_THREAD_ID))
                self.assertIsNone(codex.binding_status(TARGET_THREAD_ID))

    def test_return_receipt_accepts_attempted_current_resume_and_exact_owner(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _changed, _token = self._register(workspace)
                record = completion(binding, 9)
                claimed = codex._claim_completion(record, binding["binding_id"])
                self.assertIsNotNone(claimed)
                receipt = codex.return_receipt(
                    TARGET_THREAD_ID,
                    SOURCE_THREAD_ID,
                    workspace,
                    record["completion_id"],
                )
                with self.assertRaisesRegex(ValueError, "another source task"):
                    codex.return_receipt(
                        TARGET_THREAD_ID,
                        OTHER_SOURCE_THREAD_ID,
                        workspace,
                        record["completion_id"],
                    )
        self.assertEqual(receipt["receipt_state"], "attempted_current_resume")
        self.assertEqual(receipt["anchor"], BASELINE_ANCHOR)

    def test_status_report_distinguishes_in_flight_from_unbound(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _changed, _token = self._register(workspace)
                record = completion(binding, 9)
                codex._claim_completion(record, binding["binding_id"])
                report = codex.binding_status_report(TARGET_THREAD_ID)
                missing = codex.binding_status_report(OTHER_SOURCE_THREAD_ID)
        self.assertEqual(report["state"], "bound")
        self.assertEqual(report["in_flight_completion_ids"], [record["completion_id"]])
        self.assertEqual(missing["state"], "unbound")
        self.assertIsNone(missing["binding"])

    def test_same_source_resumes_are_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _changed, _token = self._register(workspace)
                first_started = threading.Event()
                release_first = threading.Event()
                second_started = threading.Event()
                active = 0
                max_active = 0
                guard = threading.Lock()

                def runner(_command, _cwd):
                    nonlocal active, max_active
                    with guard:
                        active += 1
                        max_active = max(max_active, active)
                        ordinal = active
                    if not first_started.is_set():
                        first_started.set()
                        release_first.wait(2)
                    else:
                        second_started.set()
                    with guard:
                        active -= 1
                    return 0

                with ThreadPoolExecutor(max_workers=2) as executor:
                    first = executor.submit(
                        codex.process_completion,
                        completion(binding, 20),
                        runner=runner,
                        command_prefix=["codex"],
                    )
                    self.assertTrue(first_started.wait(1))
                    second = executor.submit(
                        codex.process_completion,
                        completion(binding, 21),
                        runner=runner,
                        command_prefix=["codex"],
                    )
                    self.assertFalse(second_started.wait(0.1))
                    release_first.set()
                    self.assertEqual(first.result(2), "resumed")
                    self.assertEqual(second.result(2), "stale")
        self.assertEqual(max_active, 1)

    def test_source_terminal_waits_for_in_flight_attempt_before_deferred_retry(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _changed, _token = self._register(workspace)
                record = completion(binding, 22)
                first_started = threading.Event()
                release_first = threading.Event()
                retried = []
                source_record = {
                    "provider": "codex",
                    "session_id": codex._opaque_thread_id(SOURCE_THREAD_ID),
                    "phase": "completed",
                }

                def busy_runner(_command, _cwd):
                    first_started.set()
                    release_first.wait(2)
                    return 1

                with ThreadPoolExecutor(max_workers=2) as executor:
                    first = executor.submit(
                        codex.process_completion,
                        record,
                        runner=busy_runner,
                        command_prefix=["codex"],
                    )
                    self.assertTrue(first_started.wait(1))
                    retry = executor.submit(
                        codex.retry_pending_for_source,
                        source_record,
                        runner=lambda command, cwd: retried.append((command, cwd)) or 0,
                        command_prefix=["codex"],
                    )
                    self.assertFalse(retry.done())
                    release_first.set()
                    self.assertEqual(first.result(2), "deferred")
                    self.assertEqual(retry.result(2), "resumed")
                persisted = codex.binding_status(TARGET_THREAD_ID)
        self.assertEqual(len(retried), 1)
        self.assertIn(record["completion_id"], persisted["delivered_completion_ids"])

    def test_return_receipt_is_delivered_only_after_resume_exits(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _changed, _token = self._register(workspace)
                record = completion(binding, 10)
                self.assertEqual(
                    codex.process_completion(
                        record,
                        runner=lambda command, cwd: 0,
                        command_prefix=["codex"],
                    ),
                    "resumed",
                )
                receipt = codex.return_receipt(
                    TARGET_THREAD_ID,
                    SOURCE_THREAD_ID,
                    workspace,
                    record["completion_id"],
                )
        self.assertEqual(receipt["receipt_state"], "delivered")

    def test_shared_relay_dispatches_codex_without_another_listener(self) -> None:
        record = {"provider": "codex"}
        with mock.patch.object(
            codex, "retry_pending_for_source", return_value="none"
        ) as retry, mock.patch.object(
            codex, "process_completion", return_value="resumed"
        ) as process, mock.patch("return_journal.production_dispatcher") as factory:
            factory.return_value.process_event.return_value = "resumed"
            self.assertEqual(relay.process_completion_record(record), "resumed")
        factory.return_value.process_event.assert_called_once_with(record)
        retry.assert_not_called()
        process.assert_not_called()

    def test_completion_dispatcher_does_not_block_later_event_and_commits_in_order(self) -> None:
        first_started = threading.Event()
        release_first = threading.Event()
        second_finished = threading.Event()
        cursors = []

        def handler(record):
            if record["cursor"] == 1:
                first_started.set()
                release_first.wait(2)
            else:
                second_finished.set()
            return "resumed"

        dispatcher = relay._CompletionDispatcher(
            handler=handler,
            cursor_writer=cursors.append,
            max_workers=2,
        )
        dispatcher.submit({"cursor": 1})
        self.assertTrue(first_started.wait(1))
        dispatcher.submit({"cursor": 2})
        self.assertTrue(second_finished.wait(1))
        self.assertEqual(cursors, [])
        release_first.set()
        dispatcher.close()
        self.assertEqual(cursors, [1, 2])


if __name__ == "__main__":
    unittest.main()
