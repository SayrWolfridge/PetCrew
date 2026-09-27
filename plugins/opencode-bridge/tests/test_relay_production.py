"""Acceptance through real provider admission and the default stream handler."""
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from relay_runner import RunEvidence, run_stream

SOURCE = '019fd933-87b0-7d93-9666-bf02bf4f03d5'
TARGET = '019fd3ed-cb16-7362-af22-cf341aa706d5'
OTHER = '019fe6a7-09e3-75f0-a368-4eb18afd746e'


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, LOCALAPPDATA=self.tmp.name)
        patch.start()
        self.addCleanup(patch.stop)
        self.j = importlib.import_module('return_journal')
        self.p = importlib.import_module('relay_providers')
        self.o = importlib.import_module('opencode_relay')
        self.c = importlib.import_module('codex_relay')
        self.j._GLOBAL_JOURNAL = self.j.ReturnJournal()
        self.calls, self.events = [], []
        self.status = 'idle'
        self.engine = self.j.ReturnDispatcher(
            self.j.get_journal(), providers=self.p, runner=self.success,
            command_prefix=['fake-codex'], status_probe=lambda _: self.status,
            publisher=self.publish)
        self.j._PRODUCTION_DISPATCHER = self.engine
        for target, name, value in [(self.o, '_read_cursor', 0),
                                    (self.c, 'capture_thread_anchor', {'turn_id': None, 'item_hashes': {}})]:
            patch = mock.patch.object(target, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)

    def publish(self, event):
        self.events.append(event)
        return 202, ''

    def success(self, command, workspace, started):
        self.calls.append(command)
        started(None)  # Documented CLI start can omit turn_id.
        return RunEvidence(started=True, terminal_phase='completed', exit_code=0)

    def event(self, provider='opencode', index=1, source=SOURCE):
        if provider == 'opencode':
            binding = self.o.register_binding('ses_' + str(index), self.tmp.name, source)[0]
            session = binding['opaque_session_id']
        else:
            binding = self.c.register_binding(TARGET, source, self.tmp.name, 'cursor', 0,
                                              {'turn_id': None, 'item_hashes': {}})[0]
            session = binding['opaque_target_session_id']
        return dict(provider=provider, cursor=index, session_id=session,
                    agent_id='turn:' + 'b' * 64, completion_id='completion:' + f'{index:064x}',
                    phase='completed', completed_at=datetime.now(timezone.utc).isoformat())

    def terminal(self, source=SOURCE):
        return dict(provider='codex', cursor=99, session_id=self.c._opaque_thread_id(source),
                    agent_id='turn:' + 'c' * 64, completion_id='completion:' + 'f' * 64,
                    phase='completed', completed_at=datetime.now(timezone.utc).isoformat())

    def test_default_handler_both_providers_exact_finished_and_status(self):
        for provider, index in [('opencode', 1), ('codex', 2)]:
            event = self.event(provider, index)
            self.assertEqual(self.o.process_completion_record(event), 'resumed')
            self.assertIn(event['completion_id'], self.calls[-1][-1])
            self.assertEqual(self.calls[-1][-2], SOURCE)
        records = self.j.get_journal().all_records()
        self.assertEqual(len(records), 2)
        self.assertTrue(all(r.lifecycle == 'finished' and r.bookkeeping_closed for r in records))
        self.assertTrue(all(e['event_type'] == 'agent.discovered' for e in self.events))
        self.assertEqual(len({e['agent_id'] for e in self.events}), 1)
        self.assertEqual(len({e['sequence'] for e in self.events}), len(self.events))
        self.assertIn('working', [e['payload']['phase'] for e in self.events])
        self.assertTrue(all(e['payload']['navigation']['target'] == SOURCE for e in self.events))
        self.assertFalse(list(self.o.result_ready_dir().glob('*.json')))

    def test_busy_two_receipts_then_source_terminal_drains_without_message(self):
        self.status = 'active'
        for provider, index in [('opencode', 1), ('codex', 2)]:
            self.assertEqual(self.o.process_completion_record(self.event(provider, index)), 'waiting')
        self.assertEqual(len(self.j.get_journal().all_records()), 2)
        self.assertFalse(self.calls)
        self.status = 'idle'
        self.o.process_completion_record(self.terminal())
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(r.bookkeeping_closed for r in self.j.get_journal().all_records()))

    def test_write_ahead_crash_after_provider_claim_recovers_once(self):
        event = self.event()
        claim = self.p.claim
        def crash(record):
            self.assertTrue(claim(record))
            raise SystemExit('injected crash')
        with mock.patch.object(self.p, 'claim', side_effect=crash):
            with self.assertRaises(SystemExit):
                self.o.process_completion_record(event)
        self.assertFalse(self.calls)
        self.engine._journal = self.j.ReturnJournal()
        sources = self.engine.recover()
        self.assertEqual(sources, [SOURCE])
        self.engine.drain_source(SOURCE)
        self.assertEqual(len(self.calls), 1)
        self.engine.recover()
        self.engine.drain_source(SOURCE)
        self.assertEqual(len(self.calls), 1)

    def test_restart_after_launch_is_uncertain_not_rerun(self):
        def crash(*args):
            raise SystemExit('after spawn unknown')
        self.engine._runner = crash
        with self.assertRaises(SystemExit):
            self.o.process_completion_record(self.event())
        self.engine._journal = self.j.ReturnJournal()
        self.engine._runner = self.success
        self.engine.recover()
        self.assertEqual(self.engine._journal.all_records()[0].lifecycle, 'uncertain')
        self.engine.drain_source(SOURCE)
        self.assertFalse(self.calls)

    def test_outbox_failure_replay_never_invokes_runner(self):
        self.engine._publisher = lambda _: (500, '')
        self.o.process_completion_record(self.event())
        pending = list(self.j.outbox_dir().rglob('*.json'))
        self.assertEqual(len(pending), 5)
        self.assertNotIn(
            'Use opencode_read_session',
            ''.join(p.read_text(encoding='utf-8') for p in pending),
        )
        self.engine._publisher = self.publish
        self.engine.recover()
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(list(self.j.outbox_dir().rglob('*.json')))

    def test_no_start_busy_requires_positive_postprobe_and_terminal_event(self):
        def busy(*args):
            self.status = 'active'
            return RunEvidence(exit_code=1, error_class='cli_error', unstarted_busy=True)
        self.engine._runner = busy
        self.o.process_completion_record(self.event())
        self.assertEqual(self.j.get_journal().all_records()[0].lifecycle, 'waiting')
        self.engine._runner = self.success
        self.status = 'idle'
        self.o.process_completion_record(self.terminal())
        self.assertEqual(len(self.calls), 1)

    def test_production_path_source_qualified_busy_replays_once_after_terminal(self):
        """The real JSONL runner evidence drives one durable, event-driven retry."""
        attempts = []

        class Process:
            def __init__(self, output, exit_code):
                self.stdout = io.BytesIO(output)
                self.exit_code = exit_code

            def wait(self):
                return self.exit_code

        busy_output = json.dumps({
            'type': 'error',
            'message': f'Thread {SOURCE} already has an active writer',
        }).encode() + b'\n'

        def first_busy_then_success(command, workspace, started):
            attempts.append(command)
            if len(attempts) == 1:
                return run_stream(
                    command,
                    workspace,
                    started,
                    process_factory=lambda *_args, **_kwargs: Process(busy_output, 1),
                )
            return self.success(command, workspace, started)

        self.engine._runner = first_busy_then_success
        event = self.event()
        self.assertEqual(self.o.process_completion_record(event), 'waiting')
        waiting = self.j.get_journal().all_records()[0]
        self.assertEqual(waiting.lifecycle, 'waiting')
        self.assertEqual(waiting.error_class, 'unstarted_busy')
        self.assertFalse(self.calls)
        self.assertTrue(any(
            e['payload']['task']['title'].startswith('Codex пока не принимает возврат')
            for e in self.events
        ))

        self.o.process_completion_record(self.terminal())
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(self.calls), 1)
        finished = self.j.get_journal().all_records()[0]
        self.assertEqual(finished.lifecycle, 'finished')
        self.assertTrue(finished.bookkeeping_closed)
        binding = self.o._read_binding(event['session_id'])
        self.assertEqual(binding['delivered_completion_ids'], [event['completion_id']])

        self.assertEqual(self.o.process_completion_record(event), 'unrouted')
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(self.calls), 1)

    def test_after_start_missing_terminal_never_retries(self):
        def failed(command, workspace, started):
            self.calls.append(command)
            started(None)
            return RunEvidence(started=True, exit_code=1)
        self.engine._runner = failed
        self.o.process_completion_record(self.event())
        self.o.process_completion_record(self.terminal())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.j.get_journal().all_records()[0].lifecycle, 'uncertain')

    def test_transfer_during_final_status_read_does_not_wake_old_source(self):
        event = self.event()
        def transfer(_):
            self.o.register_binding('ses_1', self.tmp.name, OTHER, transfer_parent=True)
            return 'idle'
        self.engine._status_probe = transfer
        self.o.process_completion_record(event)
        self.assertFalse(self.calls)
        self.assertEqual(self.j.get_journal().all_records()[0].error_class, 'stale_binding')

    def test_duplicate_while_running_has_one_runner(self):
        event = self.event()
        entered, release = threading.Event(), threading.Event()
        def runner(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return self.success(*args)
        self.engine._runner = runner
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.o.process_completion_record, event)
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.o.process_completion_record(event), 'unrouted')
            release.set()
            self.assertEqual(first.result(3), 'resumed')
        self.assertEqual(len(self.calls), 1)

    def test_mixed_provider_same_source_serializes_and_drains_both(self):
        first_event, second_event = self.event(), self.event('codex', 2)
        entered, release = threading.Event(), threading.Event()
        def runner(*args):
            if not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(3))
            return self.success(*args)
        self.engine._runner = runner
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.o.process_completion_record, first_event)
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.o.process_completion_record(second_event), 'waiting')
            self.assertFalse(self.calls)
            release.set()
            self.assertEqual(first.result(3), 'resumed')
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(r.bookkeeping_closed for r in self.j.get_journal().all_records()))

    def test_independent_sources_can_run_concurrently(self):
        events = [self.event(), self.event(index=2, source=OTHER)]
        barrier = threading.Barrier(2)
        def runner(*args):
            barrier.wait(3)
            return self.success(*args)
        self.engine._runner = runner
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(self.o.process_completion_record, events))
        self.assertEqual(results, ['resumed', 'resumed'])
        self.assertEqual(len(self.calls), 2)

    def test_finished_before_delivery_crash_closes_without_second_model(self):
        event = self.event()
        with mock.patch.object(self.p, 'complete', side_effect=SystemExit('delivery crash')):
            with self.assertRaises(SystemExit):
                self.o.process_completion_record(event)
        self.engine._journal = self.j.ReturnJournal()
        self.engine.recover()
        record = self.engine._journal.all_records()[0]
        self.assertEqual(record.lifecycle, 'finished')
        self.assertTrue(record.bookkeeping_closed)
        self.engine.drain_source(SOURCE)
        self.assertEqual(len(self.calls), 1)

    def test_terminal_during_busy_probe_hands_wakeup_to_current_drainer(self):
        entered, release = threading.Event(), threading.Event()
        probes = []
        def probe(_):
            probes.append(1)
            if len(probes) == 1:
                entered.set()
                self.assertTrue(release.wait(3))
                return 'active'  # Status observed before the terminal event.
            return 'idle'
        self.engine._status_probe = probe
        event = self.event()
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.o.process_completion_record, event)
            self.assertTrue(entered.wait(3))
            self.o.process_completion_record(self.terminal())
            release.set()
            self.assertEqual(first.result(3), 'resumed')
        self.assertEqual(len(self.calls), 1)

    def test_writer_refusal_does_not_prove_active_model(self):
        reader = importlib.import_module('codex_app_reader')
        with mock.patch.object(reader, 'capture_thread_anchor', side_effect=RuntimeError(
                'thread ' + SOURCE + ' already has an active writer')):
            self.assertEqual(self.j.source_status_probe(SOURCE, client_factory=lambda: None), 'unavailable')
        with mock.patch.object(reader, 'capture_thread_anchor', side_effect=RuntimeError('other failure')):
            self.assertEqual(self.j.source_status_probe(SOURCE, client_factory=lambda: None), 'unavailable')

    def test_real_stream_parser_is_used_by_production_engine(self):
        class Child:
            stdout = io.BytesIO(b'{"type":"thread.started","thread_id":"x"}\n'
                                b'{"type":"turn.started"}\n'
                                b'{"type":"item.completed","item":{"text":"PRIVATE_MODEL_TOKEN"}}\n'
                                b'{"type":"turn.completed"}\n')
            def wait(self):
                return 0
        commands = []
        def factory(command, **kwargs):
            commands.append(command)
            return Child()
        self.engine._runner = lambda c, w, cb: run_stream(c, w, cb, process_factory=factory)
        self.assertEqual(self.o.process_completion_record(self.event()), 'resumed')
        self.assertEqual(commands[0][1:4], ['exec', 'resume', '--json'])
        record = self.j.get_journal().all_records()[0]
        self.assertIsNone(record.turn_id)
        self.assertEqual(record.terminal_evidence, 'completed')
        self.assertTrue(record.turn_started_at)
        for path in Path(self.tmp.name).rglob('*.json'):
            self.assertNotIn('PRIVATE_MODEL_TOKEN', path.read_text())

    def test_real_stream_reconnect_preserves_waiting_without_launch(self):
        self.status = 'active'
        self.o.process_completion_record(self.event())
        self.status = 'idle'
        response = mock.MagicMock()
        response.__enter__.return_value.readline.return_value = b''
        with mock.patch.object(self.o, '_petcrew_discover', return_value=('http://127.0.0.1:1', 'fake')), \
             mock.patch.object(self.o, '_flush_pending_result_ready'), \
             mock.patch.object(self.o.urllib.request, 'urlopen', return_value=response):
            self.o._stream_once(threading.Event())
        self.assertEqual(len(self.calls), 0)
        waiting = self.j.get_journal().all_records()[0]
        self.assertEqual(waiting.lifecycle, 'waiting')
        self.assertFalse(waiting.bookkeeping_closed)

        self.o.process_completion_record(self.terminal())
        self.assertEqual(len(self.calls), 1)
        finished = self.j.get_journal().all_records()[0]
        self.assertEqual(finished.lifecycle, 'finished')
        self.assertTrue(finished.bookkeeping_closed)

    def test_completion_cursor_does_not_ack_unpersisted_exception(self):
        cursors = []
        def handler(event):
            if event['cursor'] == 1:
                raise RuntimeError('capacity before write-ahead')
            return 'waiting'
        with mock.patch.object(self.o, '_append_log'):
            dispatcher = self.o._CompletionDispatcher(handler=handler, cursor_writer=cursors.append)
            dispatcher.submit({'cursor': 1})
            dispatcher.submit({'cursor': 2})
            dispatcher.close()
        self.assertEqual(cursors, [])

    def test_corrupt_journal_fails_closed(self):
        storage = self.j.journal_dir()
        storage.mkdir(parents=True)
        (storage / (self.j._source_key(SOURCE) + '.json')).write_text('{broken')
        with self.assertRaisesRegex(RuntimeError, 'journal_unreadable'):
            self.engine.recover()
        self.assertFalse(self.calls)


if __name__ == '__main__':
    unittest.main()
