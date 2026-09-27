from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


journal_module = _load("return_journal", "return_journal.py")
relay = _load("opencode_relay", "opencode_relay.py")
codex = _load("codex_relay", "codex_relay.py")

SOURCE_THREAD_ID = "019fd3ed-cb16-7362-af22-cf341aa706d5"
OTHER_SOURCE_THREAD_ID = "019fe6a7-09e3-75f0-a368-4eb18afd746e"
TARGET_THREAD_ID = "019f9a58-1f22-7f63-bd1c-7c480dd3ea1d"
COMPLETION_ID_1 = "completion:" + "a" * 64
COMPLETION_ID_2 = "completion:" + "b" * 64
COMPLETION_ID_3 = "completion:" + "c" * 64
BINDING_GEN = "b" * 32

from relay_runner import RunEvidence, run_stream


def completed_runner(command, workspace, on_started):
    on_started(None)
    return RunEvidence(started=True, terminal_phase="completed", exit_code=0)


def unknown_runner(command, workspace, on_started):
    on_started(None)
    return RunEvidence(started=True, exit_code=1, error_class="cli_exit_nonzero")


class ReturnJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._patch_env = mock.patch.dict(
            os.environ, {"LOCALAPPDATA": self._tmpdir}
        )
        self._patch_env.start()
        self._journal = journal_module.ReturnJournal()

    def tearDown(self) -> None:
        self._patch_env.stop()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_intake_creates_received_record(self) -> None:
        record = self._journal.intake(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        self.assertIsNotNone(record)
        self.assertEqual(record.lifecycle, journal_module.RECEIVED)
        self.assertEqual(record.provider, "codex")
        self.assertEqual(record.completion_id, COMPLETION_ID_1)

    def test_intake_deduplicates_on_completion_id(self) -> None:
        first = self._journal.intake(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        second = self._journal.intake(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        self.assertEqual(first.job_id, second.job_id)

    def test_intake_allows_different_completions(self) -> None:
        first = self._journal.intake(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        second = self._journal.intake(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_2, BINDING_GEN
        )
        self.assertNotEqual(first.job_id, second.job_id)

    def test_same_receipt_in_other_provider_or_generation_is_distinct(self) -> None:
        records = [self._journal.intake(provider, SOURCE_THREAD_ID, COMPLETION_ID_1, generation)
                   for provider, generation in (("codex", BINDING_GEN),
                                                ("opencode", BINDING_GEN),
                                                ("codex", "c" * 32))]
        self.assertEqual(len({record.job_id for record in records}), 3)

    def test_transition_updates_lifecycle(self) -> None:
        record = self._journal.intake(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        self._journal.transition(record.job_id, journal_module.WAITING)
        updated = self._journal.get_record(record.job_id)
        self.assertEqual(updated.lifecycle, journal_module.WAITING)

    def test_transition_to_launching_sets_timestamp(self) -> None:
        record = self._journal.intake(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        self._journal.transition(record.job_id, journal_module.LAUNCHING)
        updated = self._journal.get_record(record.job_id)
        self.assertIsNotNone(updated.launched_at)

    def test_persistence_survives_reload(self) -> None:
        record = self._journal.intake(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        self._journal.transition(record.job_id, journal_module.WAITING)

        new_journal = journal_module.ReturnJournal()
        active = new_journal.get_active_for_source(SOURCE_THREAD_ID)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].lifecycle, journal_module.WAITING)

    def test_trim_preserves_active_records(self) -> None:
        for i in range(80):
            cid = f"completion:{i:064x}"
            record = self._journal.intake(
                "codex", SOURCE_THREAD_ID, cid, BINDING_GEN
            )
            self._journal.transition(record.job_id, journal_module.FINISHED)
            self._journal.mark_bookkeeping(record.job_id, closed=True)

        record = self._journal.intake(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_3, BINDING_GEN
        )
        self._journal.transition(record.job_id, journal_module.WAITING)

        active = self._journal.get_active_for_source(SOURCE_THREAD_ID)
        self.assertEqual(len(active), 1)

    def test_visible_state_mapping(self) -> None:
        phase, label = journal_module.visible_state(journal_module.RECEIVED)
        self.assertEqual(phase, "queued")
        self.assertIn("\u0412\u043e\u0437\u0432\u0440\u0430\u0442", label)

        phase, label = journal_module.visible_state(journal_module.WAITING)
        self.assertEqual(phase, "queued")

        phase, label = journal_module.visible_state(journal_module.RUNNING)
        self.assertEqual(phase, "working")

        phase, label = journal_module.visible_state(journal_module.FINISHED)
        self.assertEqual(phase, "completed")

        phase, label = journal_module.visible_state(journal_module.FAILED)
        self.assertEqual(phase, "failed")

        phase, label = journal_module.visible_state(journal_module.UNCERTAIN)
        self.assertEqual(phase, "failed")

    def test_relay_agent_id_is_stable(self) -> None:
        id1 = journal_module.relay_agent_id(SOURCE_THREAD_ID)
        id2 = journal_module.relay_agent_id(SOURCE_THREAD_ID)
        self.assertEqual(id1, id2)
        self.assertRegex(id1, r"^relay:[0-9a-f]{64}$")

    def test_relay_agent_id_is_different_per_source(self) -> None:
        id1 = journal_module.relay_agent_id(SOURCE_THREAD_ID)
        id2 = journal_module.relay_agent_id(OTHER_SOURCE_THREAD_ID)
        self.assertNotEqual(id1, id2)


class DispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._patch_env = mock.patch.dict(
            os.environ, {"LOCALAPPDATA": self._tmpdir}
        )
        self._patch_env.start()
        self._journal = journal_module.ReturnJournal()

    def tearDown(self) -> None:
        self._patch_env.stop()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_idle_return_single_start_and_finish(self) -> None:
        calls = []

        def runner(command, workspace, on_started):
            calls.append(command)
            return completed_runner(command, workspace, on_started)

        dispatcher = journal_module.ReturnDispatcher(
            self._journal,
            status_probe=lambda source: "idle",
            runner=runner,
            command_prefix=["codex"],
        )

        record = dispatcher.intake_return(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        self.assertEqual(record.lifecycle, journal_module.WAITING)

        result = dispatcher.try_launch(
            "codex",
            SOURCE_THREAD_ID,
            lambda r: "test prompt",
            "/workspace",
            TARGET_THREAD_ID,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.lifecycle, journal_module.FINISHED)
        self.assertEqual(len(calls), 1)

    def test_active_source_no_runner_durable_waiting(self) -> None:
        calls = []

        def runner(command, workspace, on_started):
            calls.append(command)
            return completed_runner(command, workspace, on_started)

        dispatcher = journal_module.ReturnDispatcher(
            self._journal,
            status_probe=lambda source: "active",
            runner=runner,
            command_prefix=["codex"],
        )

        record = dispatcher.intake_return(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        self.assertEqual(record.lifecycle, journal_module.WAITING)
        self.assertEqual(len(calls), 0)

        result = dispatcher.try_launch(
            "codex",
            SOURCE_THREAD_ID,
            lambda r: "test prompt",
            "/workspace",
            TARGET_THREAD_ID,
        )
        self.assertIsNone(result)
        self.assertEqual(len(calls), 0)

    def test_monitor_pickup_closes_without_status_probe_or_runner(self) -> None:
        status_probe = mock.Mock(side_effect=AssertionError("status probe must stay off"))
        runner = mock.Mock(side_effect=AssertionError("runner must stay off"))
        dispatcher = journal_module.ReturnDispatcher(
            self._journal,
            status_probe=status_probe,
            runner=runner,
            command_prefix=["codex"],
            monitor_pickup_providers={"opencode"},
        )

        dispatcher.intake_return(
            "opencode", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        result = dispatcher.try_launch(
            "opencode",
            SOURCE_THREAD_ID,
            lambda record: "must not build prompt",
            "/workspace",
            SOURCE_THREAD_ID,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.lifecycle, journal_module.FINISHED)
        self.assertEqual(result.terminal_evidence, "monitor_pickup_ready")
        self.assertTrue(self._journal.get_record(result.job_id).bookkeeping_closed)
        status_probe.assert_not_called()
        runner.assert_not_called()

    def test_source_terminal_drains_waiting(self) -> None:
        calls = []

        def runner(command, workspace, on_started):
            calls.append(command)
            return completed_runner(command, workspace, on_started)

        dispatcher = journal_module.ReturnDispatcher(
            self._journal,
            status_probe=lambda source: "active",
            runner=runner,
            command_prefix=["codex"],
        )

        dispatcher.intake_return(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        dispatcher.intake_return(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_2, BINDING_GEN
        )

        active = self._journal.get_active_for_source(SOURCE_THREAD_ID)
        self.assertEqual(len(active), 2)
        self.assertTrue(all(r.lifecycle == journal_module.WAITING for r in active))

        dispatcher._status_probe = lambda source: "idle"
        drained = dispatcher.drain_on_terminal(
            "codex",
            SOURCE_THREAD_ID,
            lambda records: "drain prompt",
            "/workspace",
            TARGET_THREAD_ID,
        )
        self.assertEqual(len(drained), 2)
        self.assertTrue(all(r.lifecycle == journal_module.FINISHED for r in drained))
        self.assertEqual(len(calls), 2)

    def test_terminal_before_enqueue_race(self) -> None:
        status = ["active"]
        calls = []
        dispatcher = journal_module.ReturnDispatcher(
            self._journal,
            status_probe=lambda source: status[0],
            runner=lambda cmd, ws, started: calls.append(cmd) or completed_runner(cmd, ws, started),
            command_prefix=["codex"],
        )
        # Terminal notification arrived before receipt enqueue and saw no work.
        status[0] = "idle"
        self.assertEqual(dispatcher.drain_on_terminal(
            "codex", SOURCE_THREAD_ID, lambda r: "prompt", "/workspace", TARGET_THREAD_ID), [])
        record = dispatcher.intake_return(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        self.assertEqual(record.lifecycle, journal_module.WAITING)

        result = dispatcher.try_launch(
            "codex",
            SOURCE_THREAD_ID,
            lambda r: "prompt",
            "/workspace",
            TARGET_THREAD_ID,
        )
        self.assertEqual(result.lifecycle, journal_module.FINISHED)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self._journal.get_active_for_source(SOURCE_THREAD_ID), [])

    def test_post_check_desktop_start_race(self) -> None:
        status = ["idle"]
        calls = []
        def runner(cmd, ws, on_started):
            calls.append(cmd)
            if len(calls) == 1:
                status[0] = "active"
                return RunEvidence(exit_code=1, error_class="cli_error", unstarted_busy=True)
            return completed_runner(cmd, ws, on_started)
        dispatcher = journal_module.ReturnDispatcher(
            self._journal,
            status_probe=lambda source: status[0],
            runner=runner,
            command_prefix=["codex"],
        )

        record = dispatcher.intake_return(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        self.assertEqual(record.lifecycle, journal_module.WAITING)

        result = dispatcher.try_launch(
            "codex",
            SOURCE_THREAD_ID,
            lambda r: "prompt",
            "/workspace",
            TARGET_THREAD_ID,
        )
        self.assertEqual(result.lifecycle, journal_module.WAITING)
        self.assertEqual(result.error_class, "unstarted_busy")
        self.assertEqual(len(calls), 1)
        status[0] = "idle"
        drained = dispatcher.drain_on_terminal(
            "codex", SOURCE_THREAD_ID, lambda r: "prompt", "/workspace", TARGET_THREAD_ID)
        self.assertEqual([r.lifecycle for r in drained], [journal_module.FINISHED])
        self.assertEqual(len(calls), 2)

    def test_idle_snapshot_writer_collision_preserves_receipt(self) -> None:
        calls = []
        published = []
        def runner(cmd, ws, on_started):
            calls.append(cmd)
            return RunEvidence(exit_code=1, error_class="cli_error", unstarted_busy=True)
        dispatcher = journal_module.ReturnDispatcher(
            self._journal, status_probe=lambda source: "idle", runner=runner,
            command_prefix=["codex"],
            publisher=lambda event: (published.append(event) or (202, "accepted")),
        )
        record = dispatcher.intake_return(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN)
        result = dispatcher.try_launch(
            "codex", SOURCE_THREAD_ID, lambda r: "prompt", "/workspace", TARGET_THREAD_ID)
        self.assertEqual(result.lifecycle, journal_module.WAITING)
        self.assertEqual(result.error_class, "unstarted_busy")
        self.assertEqual(len(calls), 1)
        self.assertIn(record, self._journal.get_active_for_source(SOURCE_THREAD_ID))
        payload = published[-1]["payload"]
        self.assertEqual(payload["phase"], "queued")
        self.assertIsNone(payload["result"])
        self.assertIn("результат сохранён", payload["current_action"])
        self.assertNotIn("текущего хода", payload["current_action"])
        self.assertEqual(payload["task"]["title"], payload["current_action"])

    def test_stderr_busy_refusal_keeps_record_waiting(self) -> None:
        """Real subprocess writes busy phrase to stderr; dispatcher keeps WAITING."""
        import sys as _sys
        busy_script = (
            "import sys; sys.stderr.write('Error: already has an active writer\\n'); "
            "sys.stderr.flush(); sys.exit(1)"
        )
        published = []
        dispatcher = journal_module.ReturnDispatcher(
            self._journal, status_probe=lambda source: "idle",
            runner=lambda cmd, ws, on_started: run_stream(
                [_sys.executable, "-c", busy_script], ws, on_started),
            command_prefix=["codex"],
            publisher=lambda event: (published.append(event) or (202, "accepted")),
        )
        record = dispatcher.intake_return(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN)
        result = dispatcher.try_launch(
            "codex", SOURCE_THREAD_ID, lambda r: "prompt", self._tmpdir, TARGET_THREAD_ID)
        self.assertEqual(result.lifecycle, journal_module.WAITING)
        self.assertEqual(result.error_class, "unstarted_busy")
        self.assertFalse(result.bookkeeping_closed)
        self.assertIn(record, self._journal.get_active_for_source(SOURCE_THREAD_ID))
        self.assertEqual(result.exit_code, 1)

    def test_mixed_providers_serialized_for_one_source(self) -> None:
        calls = []
        lock = threading.Lock()

        def runner(command, workspace, on_started):
            with lock:
                calls.append(command)
            return completed_runner(command, workspace, on_started)

        dispatcher = journal_module.ReturnDispatcher(
            self._journal,
            status_probe=lambda source: "idle",
            runner=runner,
            command_prefix=["codex"],
        )

        opencode_record = dispatcher.intake_return(
            "opencode", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        codex_record = dispatcher.intake_return(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_2, BINDING_GEN
        )

        self.assertEqual(opencode_record.provider, "opencode")
        self.assertEqual(codex_record.provider, "codex")

        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(
                dispatcher.try_launch,
                "opencode", SOURCE_THREAD_ID,
                lambda r: "opencode prompt", "/workspace", TARGET_THREAD_ID,
            )
            f2 = executor.submit(
                dispatcher.try_launch,
                "codex", SOURCE_THREAD_ID,
                lambda r: "codex prompt", "/workspace", TARGET_THREAD_ID,
            )
            r1 = f1.result(2)
            r2 = f2.result(2)

        self.assertIsNotNone(r1)
        self.assertIsNotNone(r2)

    def test_independent_source_not_blocked(self) -> None:
        calls = []

        def runner(command, workspace, on_started):
            calls.append(command)
            return completed_runner(command, workspace, on_started)

        dispatcher = journal_module.ReturnDispatcher(
            self._journal,
            status_probe=lambda source: "idle",
            runner=runner,
            command_prefix=["codex"],
        )

        dispatcher.intake_return(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        dispatcher.intake_return(
            "codex", OTHER_SOURCE_THREAD_ID, COMPLETION_ID_2, BINDING_GEN
        )

        r1 = dispatcher.try_launch(
            "codex", SOURCE_THREAD_ID,
            lambda r: "prompt1", "/workspace", TARGET_THREAD_ID,
        )
        r2 = dispatcher.try_launch(
            "codex", OTHER_SOURCE_THREAD_ID,
            lambda r: "prompt2", "/workspace", TARGET_THREAD_ID,
        )

        self.assertIsNotNone(r1)
        self.assertIsNotNone(r2)
        self.assertEqual(r1.lifecycle, journal_module.FINISHED)
        self.assertEqual(r2.lifecycle, journal_module.FINISHED)

    def test_duplicate_receipt_intake_returns_existing(self) -> None:
        dispatcher = journal_module.ReturnDispatcher(
            self._journal,
            status_probe=lambda source: "idle",
            runner=completed_runner,
            command_prefix=["codex"],
        )

        first = dispatcher.intake_return(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        second = dispatcher.intake_return(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        self.assertEqual(first.job_id, second.job_id)

    def test_crash_recovery_reconciles_launching_records(self) -> None:
        record = self._journal.intake(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        self._journal.transition(record.job_id, journal_module.LAUNCHING)

        dispatcher = journal_module.ReturnDispatcher(
            self._journal,
            status_probe=lambda source: "idle",
            runner=completed_runner,
            command_prefix=["codex"],
        )
        reconciled = dispatcher.reconcile_startup(SOURCE_THREAD_ID)

        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0].lifecycle, journal_module.UNCERTAIN)

    def test_restart_reconnect_does_not_lose_records(self) -> None:
        record = self._journal.intake(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        self._journal.transition(record.job_id, journal_module.WAITING)

        new_journal = journal_module.ReturnJournal()
        active = new_journal.get_active_for_source(SOURCE_THREAD_ID)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].lifecycle, journal_module.WAITING)

    def test_exit_before_start_becomes_failed(self) -> None:
        def runner(command, workspace, on_started):
            return RunEvidence(error_class="spawn_failed")

        dispatcher = journal_module.ReturnDispatcher(
            self._journal,
            status_probe=lambda source: "idle",
            runner=runner,
            command_prefix=["codex"],
        )

        record = dispatcher.intake_return(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        result = dispatcher.try_launch(
            "codex", SOURCE_THREAD_ID,
            lambda r: "prompt", "/workspace", TARGET_THREAD_ID,
        )
        self.assertEqual(result.lifecycle, journal_module.FAILED)
        self.assertEqual(result.error_class, "spawn_failed")

    def test_exit_after_start_becomes_uncertain(self) -> None:
        dispatcher = journal_module.ReturnDispatcher(
            self._journal,
            status_probe=lambda source: "idle",
            runner=unknown_runner,
            command_prefix=["codex"],
        )

        record = dispatcher.intake_return(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        result = dispatcher.try_launch(
            "codex", SOURCE_THREAD_ID,
            lambda r: "prompt", "/workspace", TARGET_THREAD_ID,
        )
        self.assertEqual(result.lifecycle, journal_module.UNCERTAIN)
        self.assertEqual(result.exit_code, 1)

    def test_nonzero_exit_not_retried_blindly(self) -> None:
        calls = []

        def runner(command, workspace, on_started):
            calls.append(command)
            return RunEvidence(exit_code=1, error_class="cli_exit_nonzero")

        dispatcher = journal_module.ReturnDispatcher(
            self._journal,
            status_probe=lambda source: "idle",
            runner=runner,
            command_prefix=["codex"],
        )

        dispatcher.intake_return(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        dispatcher.try_launch(
            "codex", SOURCE_THREAD_ID,
            lambda r: "prompt", "/workspace", TARGET_THREAD_ID,
        )
        self.assertEqual(len(calls), 1)

        active = self._journal.get_active_for_source(SOURCE_THREAD_ID)
        self.assertEqual(len(active), 0)

    def test_binding_generation_mismatch_prevents_launch(self) -> None:
        providers = mock.Mock()
        providers.validate.side_effect = lambda r: r.binding_generation == "new-generation"
        calls = []
        dispatcher = journal_module.ReturnDispatcher(
            self._journal,
            status_probe=lambda source: "idle",
            runner=lambda cmd, ws, started: calls.append(cmd) or completed_runner(cmd, ws, started),
            command_prefix=["codex"],
            providers=providers,
        )

        record = dispatcher.intake_return(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, "old-generation"
        )
        self._journal.transition(record.job_id, journal_module.WAITING)

        new_gen_record = dispatcher.intake_return(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_2, "new-generation"
        )

        result = dispatcher.try_launch(
            "codex", SOURCE_THREAD_ID,
            lambda r: "prompt", "/workspace", TARGET_THREAD_ID,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.job_id, record.job_id)
        self.assertEqual(result.lifecycle, journal_module.FAILED)
        self.assertEqual(result.error_class, "stale_binding")
        self.assertEqual(calls, [])
        result = dispatcher.try_launch("codex", SOURCE_THREAD_ID, lambda r: "prompt", "/workspace", TARGET_THREAD_ID)
        self.assertEqual(result.job_id, new_gen_record.job_id)
        self.assertEqual(result.lifecycle, journal_module.FINISHED)

    def test_startup_reconcile_turns_launching_to_uncertain(self) -> None:
        record = self._journal.intake(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        self._journal.transition(record.job_id, journal_module.WAITING)
        self._journal.transition(record.job_id, journal_module.LAUNCHING)
        self._journal.transition(record.job_id, journal_module.RUNNING)

        dispatcher = journal_module.ReturnDispatcher(self._journal)
        reconciled = dispatcher.reconcile_startup(SOURCE_THREAD_ID)

        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0].lifecycle, journal_module.UNCERTAIN)

    def test_status_probe_unavailable_fails_closed_to_waiting(self) -> None:
        dispatcher = journal_module.ReturnDispatcher(
            self._journal,
            status_probe=lambda source: "unavailable",
            runner=completed_runner,
            command_prefix=["codex"],
        )

        record = dispatcher.intake_return(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        self.assertEqual(record.lifecycle, journal_module.WAITING)

    def test_pending_drain_queue(self) -> None:
        self._journal.enqueue_drain(SOURCE_THREAD_ID, "job-1")
        self._journal.enqueue_drain(SOURCE_THREAD_ID, "job-2")
        items = self._journal.pending_drain_items(SOURCE_THREAD_ID)
        self.assertEqual(items, ["job-1", "job-2"])

        self._journal.clear_pending_drain(SOURCE_THREAD_ID)
        items = self._journal.pending_drain_items(SOURCE_THREAD_ID)
        self.assertEqual(items, [])


class OpencodeRelayJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._patch_env = mock.patch.dict(
            os.environ, {"LOCALAPPDATA": self._tmpdir}
        )
        self._patch_env.start()
        journal_module._GLOBAL_JOURNAL.__init__()

    def tearDown(self) -> None:
        self._patch_env.stop()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _register_binding(self, session_id="ses_test", thread_id=SOURCE_THREAD_ID):
        return relay.register_binding(session_id, self._tmpdir, thread_id)

    def test_journal_intake_and_waiting(self) -> None:
        binding, _, _ = self._register_binding()
        record = {
            "cursor": 1,
            "completion_id": COMPLETION_ID_1,
            "provider": "opencode",
            "session_id": binding["opaque_session_id"],
            "phase": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

        result = relay.process_completion_with_journal(
            record,
            runner=completed_runner,
            command_prefix=["codex"],
            status_probe=lambda source: "active",
        )
        self.assertEqual(result, "waiting")

    def test_journal_idle_launches(self) -> None:
        binding, _, _ = self._register_binding()
        calls = []
        record = {
            "cursor": 1,
            "completion_id": COMPLETION_ID_1,
            "provider": "opencode",
            "session_id": binding["opaque_session_id"],
            "phase": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

        result = relay.process_completion_with_journal(
            record,
            runner=lambda cmd, ws, started: calls.append(cmd) or completed_runner(cmd, ws, started),
            command_prefix=["codex"],
            status_probe=lambda source: "idle",
        )
        self.assertEqual(result, "resumed")
        self.assertEqual(len(calls), 1)


class CodexRelayJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._patch_env = mock.patch.dict(
            os.environ, {"LOCALAPPDATA": self._tmpdir}
        )
        self._patch_env.start()
        journal_module._GLOBAL_JOURNAL.__init__()
        self.anchor_patch = mock.patch.object(
            codex, "capture_thread_anchor",
            return_value={"turn_id": "turn-1", "turn_status": "completed", "item_hashes": {}},
        )
        self.anchor_patch.start()

    def tearDown(self) -> None:
        self.anchor_patch.stop()
        self._patch_env.stop()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _register(self, workspace=None, source=SOURCE_THREAD_ID):
        ws = workspace or self._tmpdir
        return codex.register_binding(
            TARGET_THREAD_ID, source, ws,
            "target-cursor", 0,
            {"turn_id": None, "item_hashes": {}},
        )

    def test_journal_deferred_when_source_active(self) -> None:
        binding, _, _ = self._register()
        record = {
            "cursor": 1,
            "completion_id": COMPLETION_ID_1,
            "provider": "codex",
            "session_id": binding["opaque_target_session_id"],
            "agent_id": "turn:" + "a" * 64,
            "phase": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

        result = codex.process_completion_with_journal(
            record,
            runner=completed_runner,
            command_prefix=["codex"],
            status_probe=lambda source: "active",
        )
        self.assertEqual(result, "deferred")

    def test_journal_idle_launches(self) -> None:
        binding, _, _ = self._register()
        calls = []
        record = {
            "cursor": 1,
            "completion_id": COMPLETION_ID_1,
            "provider": "codex",
            "session_id": binding["opaque_target_session_id"],
            "agent_id": "turn:" + "a" * 64,
            "phase": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

        result = codex.process_completion_with_journal(
            record,
            runner=lambda cmd, ws, started: calls.append(cmd) or completed_runner(cmd, ws, started),
            command_prefix=["codex"],
            status_probe=lambda source: "idle",
        )
        self.assertEqual(result, "resumed")
        self.assertEqual(len(calls), 1)


class ProductionEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._patch_env = mock.patch.dict(
            os.environ, {"LOCALAPPDATA": self._tmpdir}
        )
        self._patch_env.start()
        journal_module._GLOBAL_JOURNAL.__init__()
        self._factory_patch = mock.patch.object(journal_module, "_PRODUCTION_DISPATCHER", None)
        self._factory_patch.start()
        self.addCleanup(self._factory_patch.stop)
        self._publisher_patch = mock.patch.object(journal_module, "_status_publisher", return_value=(202, ""))
        self.publisher_mock = self._publisher_patch.start()
        self.addCleanup(self._publisher_patch.stop)
        self.engine = journal_module.production_dispatcher()
        self.engine._status_probe = mock.Mock(return_value="active")
        self.engine._runner = mock.Mock(side_effect=completed_runner)
        self.engine._command_prefix = ["codex"]

    def tearDown(self) -> None:
        self._patch_env.stop()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_default_opencode_completion_becomes_monitor_pickup_ready(self) -> None:
        binding, _, _ = relay.register_binding(
            "ses_test_ep", self._tmpdir, SOURCE_THREAD_ID,
        )
        calls = []
        record = {
            "cursor": 1,
            "completion_id": COMPLETION_ID_1,
            "provider": "opencode",
            "session_id": binding["opaque_session_id"],
            "phase": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

        result = relay.process_completion_record(record)
        self.assertEqual(result, "pickup_ready")
        self.engine._runner.assert_not_called()
        self.engine._status_probe.assert_not_called()

        journal = journal_module.get_journal()
        records = [
            record
            for record in journal.all_records()
            if record.completion_id == COMPLETION_ID_1
        ]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].lifecycle, journal_module.FINISHED)
        self.assertEqual(records[0].terminal_evidence, "monitor_pickup_ready")
        self.assertTrue(records[0].claimed)
        self.assertTrue(records[0].bookkeeping_closed)
        published = [call.args[0] for call in self.publisher_mock.call_args_list]
        self.assertTrue(any(
            event["payload"]["task"]["title"]
            == "Результат OpenCode сохранён — напишите задаче забрать его"
            for event in published
        ))
        pickup_events = [event for event in published
                         if "return_receipt" in event["payload"]]
        self.assertEqual(len(pickup_events), 1)
        self.assertEqual(pickup_events[0]["payload"]["return_receipt"], {
            "session_id": "ses_test_ep",
            "completion_id": COMPLETION_ID_1,
            "phase": "completed",
            "workspace": binding["workspace"],
        })
        self.assertEqual(
            pickup_events[0]["payload"]["navigation"]["target"],
            SOURCE_THREAD_ID,
        )
        persisted = relay._read_binding(binding["opaque_session_id"])
        self.assertIn(COMPLETION_ID_1, persisted["attempted_completion_ids"])
        self.assertNotIn(COMPLETION_ID_1, persisted["delivered_completion_ids"])

    def test_source_terminal_does_not_relaunch_pickup_ready_opencode(self) -> None:
        binding, _, _ = relay.register_binding(
            "ses_test_drain", self._tmpdir, SOURCE_THREAD_ID,
        )

        intake_record = {
            "cursor": 1,
            "completion_id": COMPLETION_ID_1,
            "provider": "opencode",
            "session_id": binding["opaque_session_id"],
            "phase": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        relay.process_completion_record(intake_record)

        journal = journal_module.get_journal()
        records = [
            record
            for record in journal.all_records()
            if record.completion_id == COMPLETION_ID_1
        ]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].lifecycle, journal_module.FINISHED)
        self.engine._status_probe.return_value = "idle"
        relay.process_completion_record({"provider": "codex", "phase": "completed",
                                         "cursor": 2, "completion_id": COMPLETION_ID_2,
                                         "agent_id": "turn:" + "c" * 64,
                                         "completed_at": datetime.now(timezone.utc).isoformat(),
                                         "session_id": codex._opaque_thread_id(SOURCE_THREAD_ID)})
        self.engine._runner.assert_not_called()
        self.assertEqual(
            journal.get_record(records[0].job_id).terminal_evidence,
            "monitor_pickup_ready",
        )

    def test_startup_scan_reconciles(self) -> None:
        journal = journal_module.get_journal()
        source_key = journal_module._source_key(SOURCE_THREAD_ID)
        sj = journal._get_journal(source_key)

        record = journal_module.ReturnRecord(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN,
        )
        record.lifecycle = journal_module.LAUNCHING
        record.codex_thread_id = SOURCE_THREAD_ID
        sj.add(record)
        sj._persist()

        reconciled = journal.startup_scan()
        self.assertTrue(len(reconciled) >= 1)
        self.assertEqual(reconciled[0].lifecycle, journal_module.UNCERTAIN)

    def test_outbox_replay_makes_zero_runner_calls(self) -> None:
        outbox = journal_module.StatusOutbox()
        journal = journal_module.get_journal()
        record = journal_module.ReturnRecord(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN,
        )
        record.lifecycle = journal_module.WAITING
        record.codex_thread_id = SOURCE_THREAD_ID
        record.sequence = 1

        source_key = journal_module._source_key(SOURCE_THREAD_ID)
        path = journal_module._outbox_path(source_key, 1)
        journal_module._atomic_json_write(
            path, {"schema_version": 1, "event": {"test": True}}
        )

        calls = []
        published = outbox.replay_on_reconnect(
            lambda event: (202, "ok")
        )
        self.assertEqual(published, 1)
        self.assertFalse(path.exists())
        self.engine._runner.assert_not_called()


class ConcurrencyBarrierTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._patch_env = mock.patch.dict(
            os.environ, {"LOCALAPPDATA": self._tmpdir}
        )
        self._patch_env.start()
        self._journal = journal_module.ReturnJournal()

    def tearDown(self) -> None:
        self._patch_env.stop()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_max_concurrent_runner_for_one_source_is_one(self) -> None:
        barrier = threading.Barrier(2, timeout=5)
        running_count = [0]
        max_running = [0]
        lock = threading.Lock()

        def runner(command, workspace, on_started):
            with lock:
                running_count[0] += 1
                max_running[0] = max(max_running[0], running_count[0])
            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass
            with lock:
                running_count[0] -= 1
            return completed_runner(command, workspace, on_started)

        dispatcher = journal_module.ReturnDispatcher(
            self._journal,
            status_probe=lambda source: "idle",
            runner=runner,
            command_prefix=["codex"],
        )

        dispatcher.intake_return(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        dispatcher.intake_return(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_2, BINDING_GEN
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(
                dispatcher.try_launch,
                "codex", SOURCE_THREAD_ID,
                lambda r: "prompt1", "/workspace", TARGET_THREAD_ID,
            )
            f2 = executor.submit(
                dispatcher.try_launch,
                "codex", SOURCE_THREAD_ID,
                lambda r: "prompt2", "/workspace", TARGET_THREAD_ID,
            )
            f1.result(10)
            f2.result(10)

        self.assertEqual(max_running[0], 1)

    def test_different_source_can_overlap(self) -> None:
        barrier = threading.Barrier(2, timeout=5)
        running_count = [0]
        max_running = [0]
        lock = threading.Lock()

        def runner(command, workspace, on_started):
            with lock:
                running_count[0] += 1
                max_running[0] = max(max_running[0], running_count[0])
            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass
            with lock:
                running_count[0] -= 1
            return completed_runner(command, workspace, on_started)

        dispatcher = journal_module.ReturnDispatcher(
            self._journal,
            status_probe=lambda source: "idle",
            runner=runner,
            command_prefix=["codex"],
        )

        dispatcher.intake_return(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        dispatcher.intake_return(
            "codex", OTHER_SOURCE_THREAD_ID, COMPLETION_ID_2, BINDING_GEN
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(
                dispatcher.try_launch,
                "codex", SOURCE_THREAD_ID,
                lambda r: "prompt1", "/workspace", TARGET_THREAD_ID,
            )
            f2 = executor.submit(
                dispatcher.try_launch,
                "codex", OTHER_SOURCE_THREAD_ID,
                lambda r: "prompt2", "/workspace", TARGET_THREAD_ID,
            )
            f1.result(10)
            f2.result(10)

        self.assertEqual(max_running[0], 2)


class CrashInjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._patch_env = mock.patch.dict(
            os.environ, {"LOCALAPPDATA": self._tmpdir}
        )
        self._patch_env.start()

    def tearDown(self) -> None:
        self._patch_env.stop()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_crash_after_intake_before_claim_reload(self) -> None:
        journal = journal_module.ReturnJournal()
        record = journal.intake(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        journal.transition(record.job_id, journal_module.WAITING)

        new_journal = journal_module.ReturnJournal()
        active = new_journal.get_active_for_source(SOURCE_THREAD_ID)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].lifecycle, journal_module.WAITING)
        self.assertEqual(active[0].completion_id, COMPLETION_ID_1)

    def test_crash_during_launch_reload(self) -> None:
        journal = journal_module.ReturnJournal()
        record = journal.intake(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        journal.transition(record.job_id, journal_module.WAITING)
        journal.transition(record.job_id, journal_module.LAUNCHING)

        new_journal = journal_module.ReturnJournal()
        dispatcher = journal_module.ReturnDispatcher(new_journal)
        reconciled = dispatcher.reconcile_startup(SOURCE_THREAD_ID)
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0].lifecycle, journal_module.UNCERTAIN)

    def test_duplicate_intake_idempotent(self) -> None:
        journal = journal_module.ReturnJournal()
        r1 = journal.intake(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        r2 = journal.intake(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        self.assertEqual(r1.job_id, r2.job_id)
        active = journal.get_active_for_source(SOURCE_THREAD_ID)
        self.assertEqual(len(active), 1)


class JsonlEventParsingTests(unittest.TestCase):
    def test_thread_started_ignored(self) -> None:
        lines = [
            '{"type":"thread.started","thread_id":"t1"}',
            '{"type":"turn.started","turn_id":"turn-abc","started_at":"2026-09-05T10:00:00Z"}',
            '{"type":"turn.completed"}',
        ]
        result = journal_module.parse_jsonl_events(lines)
        self.assertTrue(result["started"])
        self.assertEqual(result["turn_id"], "turn-abc")
        self.assertTrue(result["terminal"])
        self.assertEqual(result["terminal_phase"], "completed")

    def test_model_text_not_retained(self) -> None:
        lines = [
            '{"type":"turn.started","turn_id":"turn-xyz"}',
            '{"type":"turn.delta","text":"Hello world this is model output"}',
            '{"type":"turn.delta","text":"More text here"}',
            '{"type":"turn.completed"}',
        ]
        result = journal_module.parse_jsonl_events(lines)
        self.assertTrue(result["started"])
        self.assertEqual(result["model_text_bytes"], len(b"Hello world this is model output") + len(b"More text here"))
        self.assertTrue(result["terminal"])
        self.assertNotIn("Hello", str(result))

    def test_error_event_classified(self) -> None:
        lines = [
            '{"type":"turn.started","turn_id":"turn-err"}',
            '{"type":"error","error_class":"timeout"}',
        ]
        result = journal_module.parse_jsonl_events(lines)
        self.assertTrue(result["started"])
        self.assertTrue(result["terminal"])
        self.assertEqual(result["terminal_phase"], "failed")
        self.assertEqual(result["error_class"], "timeout")

    def test_turn_failed_event(self) -> None:
        lines = [
            '{"type":"turn.started","turn_id":"turn-fail"}',
            '{"type":"turn.failed"}',
        ]
        result = journal_module.parse_jsonl_events(lines)
        self.assertTrue(result["terminal"])
        self.assertEqual(result["terminal_phase"], "failed")

    def test_empty_lines_produce_no_start(self) -> None:
        result = journal_module.parse_jsonl_events([])
        self.assertFalse(result["started"])
        self.assertFalse(result["terminal"])

    def test_malformed_json_ignored(self) -> None:
        lines = [
            'not json at all',
            '{"type":"turn.started","turn_id":"turn-ok"}',
            '{broken json',
        ]
        result = journal_module.parse_jsonl_events(lines)
        self.assertTrue(result["started"])
        self.assertEqual(result["turn_id"], "turn-ok")


class TransitionEnforcementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._patch_env = mock.patch.dict(
            os.environ, {"LOCALAPPDATA": self._tmpdir}
        )
        self._patch_env.start()
        self._journal = journal_module.ReturnJournal()

    def tearDown(self) -> None:
        self._patch_env.stop()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_invalid_transition_rejected(self) -> None:
        record = self._journal.intake(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        result = self._journal.transition(record.job_id, journal_module.RUNNING)
        self.assertIsNone(result)
        updated = self._journal.get_record(record.job_id)
        self.assertEqual(updated.lifecycle, journal_module.RECEIVED)

    def test_valid_transition_accepted(self) -> None:
        record = self._journal.intake(
            "codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN
        )
        result = self._journal.transition(record.job_id, journal_module.WAITING)
        self.assertIsNotNone(result)
        self.assertEqual(result.lifecycle, journal_module.WAITING)

    def test_global_limit_enforced(self) -> None:
        # Keep the actual global-capacity branch reachable below the source limit.
        with mock.patch.object(journal_module, "MAX_JOURNAL_RECORDS", 4):
            for i in range(14):
                record = self._journal.intake("codex", SOURCE_THREAD_ID,
                                              f"completion:{i:064x}", BINDING_GEN)
                self._journal.transition(record.job_id, journal_module.FINISHED)
                self._journal.mark_bookkeeping(record.job_id, closed=True)
            self.assertEqual(self._journal.count_all(), 4)

    def test_global_capacity_keeps_active_records_across_sources(self) -> None:
        with mock.patch.object(journal_module, "MAX_JOURNAL_RECORDS", 2):
            first = self._journal.intake("codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN)
            second = self._journal.intake("codex", OTHER_SOURCE_THREAD_ID, COMPLETION_ID_2, BINDING_GEN)
            with self.assertRaisesRegex(RuntimeError, "relay_global_capacity"):
                self._journal.intake("codex", OTHER_SOURCE_THREAD_ID, COMPLETION_ID_3, BINDING_GEN)
            self.assertEqual(self._journal.count_all(), 2)
            self.assertIsNotNone(self._journal.get_record(first.job_id))
            self.assertIsNotNone(self._journal.get_record(second.job_id))

    def test_capacity_refuses_without_dropping_active_or_unclosed_records(self) -> None:
        with mock.patch.object(journal_module, "MAX_RECEIPT_SUMMARIES_PER_SOURCE", 2):
            active = self._journal.intake("codex", SOURCE_THREAD_ID, COMPLETION_ID_1, BINDING_GEN)
            unclosed = self._journal.intake("codex", SOURCE_THREAD_ID, COMPLETION_ID_2, BINDING_GEN)
            self._journal.transition(unclosed.job_id, journal_module.FINISHED)
            with self.assertRaisesRegex(RuntimeError, "relay_source_capacity"):
                self._journal.intake("codex", SOURCE_THREAD_ID, COMPLETION_ID_3, BINDING_GEN)
            self.assertEqual(self._journal.count_all(), 2)
            self.assertIsNotNone(self._journal.get_record(active.job_id))
            self.assertIsNotNone(self._journal.get_record(unclosed.job_id))
            self._journal.mark_bookkeeping(unclosed.job_id, closed=True)
            self._journal.intake("codex", SOURCE_THREAD_ID, COMPLETION_ID_3, BINDING_GEN)
            self.assertIsNotNone(self._journal.get_record(active.job_id))
            self.assertIsNone(self._journal.get_record(unclosed.job_id))


if __name__ == "__main__":
    unittest.main()
