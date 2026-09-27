from __future__ import annotations

from dataclasses import asdict
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from relay_runner import MAX_LINE_BYTES, run_stream


def encoded(*events):
    return b"".join(json.dumps(event).encode() + b"\n" for event in events)


class Process:
    def __init__(self, output=b"", exit_code=0):
        self.stdout = io.BytesIO(output)
        self.exit_code = exit_code
        self.waited = False
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.exit_code if self.waited else None

    def wait(self, timeout=None):
        self.waited = True
        return self.exit_code

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class RunnerTests(unittest.TestCase):
    def run_process(self, process, callback=lambda _: None):
        def factory(command, **kwargs):
            self.assertEqual(command, ["fake-codex"])
            self.assertEqual(kwargs["cwd"], "workspace")
            self.assertEqual(kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(kwargs["stderr"], subprocess.PIPE)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            return process
        result = run_stream(["fake-codex"], "workspace", callback, process_factory=factory)
        self.assertTrue(process.waited)
        self.assertTrue(process.stdout.closed)
        return result

    def test_start_callback_precedes_next_incremental_read(self):
        started = []
        class Incremental(io.BytesIO):
            def readline(stream, size=-1):
                self.assertGreater(size, 0)
                if stream.tell():
                    self.assertEqual(started, [None])
                return super(Incremental, stream).readline(size)
            def read(stream, *args):
                self.fail("runner must not buffer entire output")
        process = Process()
        process.stdout = Incremental(encoded({"type": "turn.started"}, {"type": "turn.completed"}))
        result = self.run_process(process, started.append)
        self.assertTrue(result.started)
        self.assertEqual(result.terminal_phase, "completed")
        self.assertIsNone(result.turn_id)

    def test_thread_started_does_not_prove_turn_and_output_is_not_retained(self):
        process = Process(encoded({"type": "thread.started", "thread_id": "secret"},
                                  {"type": "item.completed", "item": {"text": "SECRET PROMPT"}}))
        result = self.run_process(process)
        self.assertFalse(result.started)
        self.assertNotIn("SECRET", repr(asdict(result)))
        self.assertEqual(len(asdict(result)), 6)

    def test_only_uuid_turn_ids_and_callback_once(self):
        valid = "019fd3ed-cb16-7362-af22-cf341aa706d5"
        for supplied, expected in ((valid, valid), ("private message", None), ([], None)):
            calls = []
            result = self.run_process(Process(encoded(
                {"type": "turn.started", "turn_id": supplied},
                {"type": "turn.started", "turn_id": valid})), calls.append)
            self.assertEqual(calls, [expected])
            self.assertEqual(result.turn_id, expected)

    def test_busy_requires_structured_prestart_phrase(self):
        for event, busy in (({"type": "error", "message": "Thread already has an active writer"}, True),
                            ({"type": "error", "message": "busy"}, False),
                            ({"type": "item.completed", "message": "already has an active writer"}, False)):
            result = self.run_process(Process(encoded(event), 1))
            self.assertEqual(result.unstarted_busy, busy)
            self.assertNotIn("writer", repr(asdict(result)))
        self.assertFalse(self.run_process(Process(b"", 1)).unstarted_busy)

    def test_after_start_failure_never_busy(self):
        result = self.run_process(Process(encoded(
            {"type": "turn.started"},
            {"type": "turn.failed", "error": {"message": "already has an active writer"}}), 1))
        self.assertTrue(result.started)
        self.assertFalse(result.unstarted_busy)
        self.assertEqual(result.terminal_phase, "failed")
        self.assertEqual(result.error_class, "turn_failed")

    def test_malformed_and_huge_lines_discarded_then_resume(self):
        data = b"invalid\n[]\n\xff\n" + b"x" * (MAX_LINE_BYTES * 4) + b"\n"
        data += encoded({"type": "turn.started"}, {"type": "turn.cancelled"})
        result = self.run_process(Process(data, 1))
        self.assertTrue(result.started)
        self.assertEqual(result.terminal_phase, "cancelled")

    def test_callback_exception_stops_and_reaps_child(self):
        process = Process(encoded({"type": "turn.started"}))
        def callback(_):
            raise RuntimeError("SECRET")
        result = self.run_process(process, callback)
        self.assertTrue(process.terminated)
        self.assertTrue(result.started)
        self.assertEqual(result.error_class, "callback_failed")
        self.assertFalse(result.unstarted_busy)

    def test_read_exception_is_conservative_even_before_start(self):
        class Broken(io.BytesIO):
            def readline(self, size=-1):
                raise OSError("SECRET")
        process = Process()
        process.stdout = Broken()
        result = self.run_process(process)
        self.assertTrue(process.terminated)
        self.assertFalse(result.unstarted_busy)
        self.assertEqual(result.error_class, "stream_read_failed")

    def test_spawn_exception_keeps_no_message(self):
        def factory(*args, **kwargs):
            raise OSError("SECRET")
        result = run_stream([], ".", lambda _: None, process_factory=factory)
        self.assertEqual(result.error_class, "spawn_failed")
        self.assertIsNone(result.exit_code)
        self.assertNotIn("SECRET", repr(asdict(result)))

    def test_reap_escalates_only_its_child_on_timeout(self):
        class Slow(Process):
            def wait(self, timeout=None):
                if timeout is not None:
                    raise subprocess.TimeoutExpired("SECRET", timeout)
                return super().wait()
        process = Slow(encoded({"type": "turn.started"}))
        def callback(_):
            raise RuntimeError()
        self.run_process(process, callback)
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)

    def test_wait_exception_clears_busy_and_reaps(self):
        class BrokenWait(Process):
            def wait(self, timeout=None):
                if timeout is None:
                    raise OSError("SECRET")
                return super().wait(timeout)
        process = BrokenWait(encoded({"type": "error", "message": "already has an active writer"}), 1)
        result = self.run_process(process)
        self.assertEqual(result.error_class, "process_wait_failed")
        self.assertFalse(result.unstarted_busy)
        self.assertTrue(process.terminated)

    def test_cleanup_tries_kill_when_terminate_fails(self):
        class BrokenTerminate(Process):
            def terminate(self):
                raise OSError("SECRET")
        process = BrokenTerminate(encoded({"type": "turn.started"}))
        def callback(_):
            raise RuntimeError()
        result = self.run_process(process, callback)
        self.assertTrue(process.killed)
        self.assertEqual(result.error_class, "callback_failed")


class StderrBusyDetectionTests(unittest.TestCase):
    def test_stderr_busy_phrase_sets_unstarted_busy(self):
        busy_script = (
            "import sys; sys.stderr.write('Error: thread already has an active writer\\n'); "
            "sys.stderr.flush(); sys.exit(1)"
        )
        result = run_stream(
            [sys.executable, "-c", busy_script], ".", lambda _: None,
        )
        self.assertTrue(result.unstarted_busy)
        self.assertFalse(result.started)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.error_class, "cli_exit_nonzero")

    def test_stderr_busy_case_insensitive(self):
        busy_script = (
            "import sys; sys.stderr.write('Thread ALREADY HAS AN ACTIVE WRITER\\n'); "
            "sys.stderr.flush(); sys.exit(1)"
        )
        result = run_stream(
            [sys.executable, "-c", busy_script], ".", lambda _: None,
        )
        self.assertTrue(result.unstarted_busy)
        self.assertFalse(result.started)

    def test_stderr_without_busy_phrase_not_busy(self):
        error_script = (
            "import sys; sys.stderr.write('some other error\\n'); "
            "sys.stderr.flush(); sys.exit(1)"
        )
        result = run_stream(
            [sys.executable, "-c", error_script], ".", lambda _: None,
        )
        self.assertFalse(result.unstarted_busy)
        self.assertFalse(result.started)
        self.assertEqual(result.exit_code, 1)

    def test_stdout_turn_started_suppresses_stderr_busy(self):
        mixed = encoded({"type": "turn.started"}, {"type": "turn.completed"})
        busy_stderr = b"already has an active writer\n"
        class StdoutOnlyProcess:
            def __init__(self):
                self.stdout = io.BytesIO(mixed)
                self.stderr = io.BytesIO(busy_stderr)
                self.exit_code = 0
                self.waited = False
                self.terminated = False
                self.killed = False
            def poll(self):
                return self.exit_code if self.waited else None
            def wait(self, timeout=None):
                self.waited = True
                return self.exit_code
            def terminate(self):
                self.terminated = True
            def kill(self):
                self.killed = True
        process = StdoutOnlyProcess()
        result = run_stream(["cmd"], ".", lambda _: None,
                            process_factory=lambda *a, **kw: process)
        self.assertTrue(result.started)
        self.assertFalse(result.unstarted_busy)
        self.assertTrue(process.waited)

    def test_stderr_drain_does_not_leak_text(self):
        busy_script = (
            "import sys; sys.stderr.write('already has an active writer\\n'); "
            "sys.stderr.flush(); sys.exit(1)"
        )
        result = run_stream(
            [sys.executable, "-c", busy_script], ".", lambda _: None,
        )
        self.assertTrue(result.unstarted_busy)
        repr_result = repr(result)
        self.assertNotIn("active writer", repr_result)
        self.assertNotIn("already", repr_result)

    def test_overcap_stderr_fully_consumed_no_deadlock(self):
        """A stderr stream exceeding the inspection cap is fully drained."""
        overcap_script = (
            "import sys, os\n"
            "sys.stderr.buffer.write(b'already has an active writer\\n')\n"
            "sys.stderr.buffer.write(os.urandom(512 * 1024))\n"
            "sys.stderr.buffer.flush()\n"
            "sys.exit(1)\n"
        )
        result = run_stream(
            [sys.executable, "-c", overcap_script], ".", lambda _: None,
        )
        self.assertTrue(result.unstarted_busy)
        self.assertFalse(result.started)
        self.assertEqual(result.exit_code, 1)
        self.assertNotIn("active writer", repr(result))

    def test_overcap_stderr_without_phrase_not_busy(self):
        """Over-cap stderr without the busy phrase drains without deadlock."""
        overcap_script = (
            "import sys, os\n"
            "sys.stderr.buffer.write(b'some unrelated error output\\n')\n"
            "sys.stderr.buffer.write(os.urandom(512 * 1024))\n"
            "sys.stderr.buffer.flush()\n"
            "sys.exit(1)\n"
        )
        result = run_stream(
            [sys.executable, "-c", overcap_script], ".", lambda _: None,
        )
        self.assertFalse(result.unstarted_busy)
        self.assertFalse(result.started)
        self.assertEqual(result.exit_code, 1)


if __name__ == "__main__":
    unittest.main()
