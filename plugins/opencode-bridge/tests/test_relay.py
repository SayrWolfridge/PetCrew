from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "opencode_relay.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("opencode_relay", SCRIPT)
assert SPEC and SPEC.loader
relay = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = relay
SPEC.loader.exec_module(relay)


THREAD_ID = "019f9a58-1f22-7f63-bd1c-7c480dd3ea1d"
OTHER_THREAD_ID = "019f7fc8-2037-7b70-8dcd-b4439ed953e9"


def completion(binding, cursor, *, completed_at=None, completion_id=None):
    return {
        "cursor": cursor,
        "completion_id": completion_id or f"completion:{cursor:064x}",
        "provider": "opencode",
        "session_id": binding["opaque_session_id"],
        "phase": "completed",
        "completed_at": completed_at or datetime.now(timezone.utc).isoformat(),
    }


class RelayTests(unittest.TestCase):
    def test_register_binding_uses_explicit_parent_and_opaque_filename(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, changed, previous = relay.register_binding(
                    "ses_test", workspace, THREAD_ID
                )
                files = list(relay.binding_dir().glob("*.json"))
        self.assertTrue(changed)
        self.assertIsNone(previous)
        self.assertEqual(len(files), 1)
        self.assertNotIn("ses_test", files[0].name)
        self.assertEqual(binding["codex_thread_id"], THREAD_ID)
        self.assertEqual(binding["opaque_session_id"], relay._opaque_session_id("ses_test"))

    def test_invalid_thread_id_does_not_register_binding(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                with self.assertRaisesRegex(ValueError, "valid codex_thread_id"):
                    relay.register_binding("ses_test", workspace, "")
                self.assertEqual(list(relay.binding_dir().glob("*.json")), [])

    def test_two_completions_resume_same_parent_without_reregistering(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _changed, _previous = relay.register_binding(
                    "ses_test", workspace, THREAD_ID
                )
                captured = []

                def runner(command, cwd):
                    captured.append((command, cwd))
                    return 0

                first = relay.process_completion(
                    completion(binding, 12), runner=runner, command_prefix=["codex"]
                )
                second = relay.process_completion(
                    completion(binding, 13), runner=runner, command_prefix=["codex"]
                )
                persisted = relay._read_binding(binding["opaque_session_id"])
        self.assertEqual((first, second), ("resumed", "resumed"))
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0][0][0:4], ["codex", "exec", "resume", THREAD_ID])
        self.assertIn("ses_test", captured[0][0][4])
        self.assertEqual(captured[0][1], str(Path(workspace).resolve()))
        self.assertEqual(persisted["last_attempted_cursor"], 13)
        self.assertEqual(persisted["after_cursor"], 13)
        self.assertEqual(len(persisted["delivered_completion_ids"]), 2)

    def test_successful_resume_publishes_one_result_ready_card(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _changed, _previous = relay.register_binding(
                    "ses_test", workspace, THREAD_ID
                )
                published = []
                record = completion(binding, 21)
                result = relay.process_completion(
                    record,
                    runner=lambda command, cwd: 0,
                    command_prefix=["codex"],
                    result_ready_publisher=lambda current_binding, current_record: published.append(
                        (current_binding, current_record)
                    )
                    or True,
                )

        self.assertEqual(result, "resumed")
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0][0]["codex_thread_id"], THREAD_ID)
        self.assertEqual(published[0][1]["completion_id"], record["completion_id"])

    def test_result_ready_event_is_deterministic_content_free_and_navigable(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _changed, _previous = relay.register_binding(
                    "ses_private", workspace, THREAD_ID
                )
                record = completion(binding, 22)
                first = relay._result_ready_event(
                    binding, record, occurred_at="2026-08-30T03:00:00+00:00"
                )
                second = relay._result_ready_event(
                    binding, record, occurred_at="2026-08-30T03:01:00+00:00"
                )

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(first["agent_id"], second["agent_id"])
        self.assertEqual(first["event_type"], "agent.discovered")
        self.assertEqual(first["payload"]["phase"], "completed")
        self.assertTrue(first["payload"]["result"]["unread"])
        self.assertEqual(first["payload"]["navigation"]["target"], THREAD_ID)
        self.assertTrue(relay._valid_result_ready_event(first))
        serialized = json.dumps(first, ensure_ascii=False)
        self.assertNotIn("ses_private", serialized)
        self.assertNotIn(str(Path(workspace).resolve()), serialized)

    def test_pending_result_ready_replays_on_core_reconnect(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _changed, _previous = relay.register_binding(
                    "ses_test", workspace, THREAD_ID
                )
                record = completion(binding, 23)
                with mock.patch.object(
                    relay, "_petcrew_discover", side_effect=RuntimeError("offline")
                ):
                    self.assertFalse(relay._publish_result_ready(binding, record))
                pending = list(relay.result_ready_dir().glob("*.json"))
                self.assertEqual(len(pending), 1)
                sent = []
                with mock.patch.object(
                    relay,
                    "_send_result_ready",
                    side_effect=lambda event, endpoint, secret: sent.append(event),
                ):
                    replayed = relay._flush_pending_result_ready(
                        "http://127.0.0.1:12345", "a" * 64
                    )
                remaining = list(relay.result_ready_dir().glob("*.json"))

        self.assertEqual(replayed, 1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(remaining, [])

    def test_result_ready_replay_conflict_counts_as_success(self) -> None:
        event = {
            "event_id": "relay-result:" + "a" * 64,
        }

        def conflict(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 409, "duplicate", {}, None)

        relay._send_result_ready(
            event,
            "http://127.0.0.1:12345",
            "b" * 64,
            opener=conflict,
        )

    def test_same_cursor_is_attempted_at_most_once(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _changed, _previous = relay.register_binding(
                    "ses_test", workspace, THREAD_ID
                )
                calls = []
                record = completion(binding, 4)
                self.assertEqual(
                    relay.process_completion(
                        record,
                        runner=lambda command, cwd: calls.append(command) or 0,
                        command_prefix=["codex"],
                    ),
                    "resumed",
                )
                self.assertEqual(
                    relay.process_completion(
                        record,
                        runner=lambda command, cwd: self.fail("duplicate wake"),
                        command_prefix=["codex"],
                    ),
                    "stale",
                )
        self.assertEqual(len(calls), 1)

    def test_same_terminal_receipt_with_a_new_cursor_is_delivered_once(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _changed, _previous = relay.register_binding(
                    "ses_test", workspace, THREAD_ID
                )
                receipt = "completion:" + "a" * 64
                self.assertEqual(
                    relay.process_completion(
                        completion(binding, 4, completion_id=receipt),
                        runner=lambda command, cwd: 0,
                        command_prefix=["codex"],
                    ),
                    "resumed",
                )
                self.assertEqual(
                    relay.process_completion(
                        completion(binding, 5, completion_id=receipt),
                        runner=lambda command, cwd: self.fail("semantic duplicate wake"),
                        command_prefix=["codex"],
                    ),
                    "stale",
                )

    def test_failed_resume_is_not_retried_but_next_cursor_is(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _changed, _previous = relay.register_binding(
                    "ses_test", workspace, THREAD_ID
                )
                record = completion(binding, 15)
                self.assertEqual(
                    relay.process_completion(
                        record, runner=lambda command, cwd: 1, command_prefix=["codex"]
                    ),
                    "failed",
                )
                self.assertEqual(
                    relay.process_completion(
                        record,
                        runner=lambda command, cwd: self.fail("failed cursor retried"),
                        command_prefix=["codex"],
                    ),
                    "stale",
                )
                self.assertEqual(
                    relay.process_completion(
                        completion(binding, 16),
                        runner=lambda command, cwd: 0,
                        command_prefix=["codex"],
                    ),
                    "resumed",
                )

    def test_completion_older_than_binding_is_not_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _changed, _previous = relay.register_binding(
                    "ses_test", workspace, THREAD_ID
                )
                record = completion(
                    binding,
                    3,
                    completed_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                )
                result = relay.process_completion(
                    record,
                    runner=lambda command, cwd: self.fail("runner should not be called"),
                    command_prefix=["codex"],
                )
        self.assertEqual(result, "stale")

    def test_same_owner_registration_preserves_attempted_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _changed, _previous = relay.register_binding(
                    "ses_test", workspace, THREAD_ID
                )
                relay.process_completion(
                    completion(binding, 7),
                    runner=lambda command, cwd: 0,
                    command_prefix=["codex"],
                )
                same, changed, previous = relay.register_binding(
                    "ses_test", workspace, THREAD_ID
                )
        self.assertFalse(changed)
        self.assertIsNone(previous)
        self.assertEqual(same["binding_id"], binding["binding_id"])
        self.assertEqual(same["last_attempted_cursor"], 7)
        self.assertEqual(same["after_cursor"], 7)
        self.assertEqual(same["delivered_completion_ids"], [f"completion:{7:064x}"])

    def test_cross_parent_requires_explicit_transfer(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                original, _changed, _previous = relay.register_binding(
                    "ses_test", workspace, THREAD_ID
                )
                with self.assertRaisesRegex(ValueError, "transfer_parent=true"):
                    relay.register_binding("ses_test", workspace, OTHER_THREAD_ID)
                transferred, changed, previous = relay.register_binding(
                    "ses_test", workspace, OTHER_THREAD_ID, transfer_parent=True
                )
        self.assertTrue(changed)
        self.assertEqual(previous["binding_id"], original["binding_id"])
        self.assertEqual(transferred["codex_thread_id"], OTHER_THREAD_ID)

    def test_detach_requires_current_parent(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                binding, _changed, _previous = relay.register_binding(
                    "ses_test", workspace, THREAD_ID
                )
                with self.assertRaisesRegex(ValueError, "another Codex task"):
                    relay.detach_binding("ses_test", OTHER_THREAD_ID)
                self.assertTrue(relay.detach_binding("ses_test", THREAD_ID))
                self.assertIsNone(relay._read_binding(binding["opaque_session_id"]))

    def test_legacy_route_migrates_and_remains_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as app_data, tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                opaque = relay._opaque_session_id("ses_test")
                legacy = {
                    "schema_version": 1,
                    "route_id": "legacy-route",
                    "opencode_session_id": "ses_test",
                    "opaque_session_id": opaque,
                    "codex_thread_id": THREAD_ID,
                    "workspace": str(Path(workspace).resolve()),
                    "armed_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                }
                relay._atomic_json_write(relay._route_path("ses_test"), legacy)
                self.assertEqual(
                    relay.process_completion(
                        completion(legacy, 20),
                        runner=lambda command, cwd: 0,
                        command_prefix=["codex"],
                    ),
                    "resumed",
                )
                migrated = relay._read_binding(opaque)
        self.assertFalse(relay._route_path("ses_test").exists())
        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(migrated["last_attempted_cursor"], 20)
        self.assertEqual(migrated["after_cursor"], 20)


if __name__ == "__main__":
    unittest.main()
