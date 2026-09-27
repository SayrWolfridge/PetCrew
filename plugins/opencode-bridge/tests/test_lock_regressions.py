from __future__ import annotations

import importlib.util
import multiprocessing
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load_codex_relay():
    spec = importlib.util.spec_from_file_location("codex_relay_lock_tests", SCRIPTS / "codex_relay.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


codex = _load_codex_relay()
TARGET_THREAD_ID = "01a07246-023e-7fe1-91ef-a0ca77a4345d"


def _child_hold_lock(app_data: str, signal_path: str, hold_seconds: float) -> None:
    os.environ["LOCALAPPDATA"] = app_data
    module = _load_codex_relay()
    lock = module._binding_lock_for_opaque(module._opaque_thread_id(TARGET_THREAD_ID))
    with lock:
        Path(signal_path).write_text("locked", encoding="utf-8")
        time.sleep(hold_seconds)


def _child_exit_while_holding(app_data: str, signal_path: str) -> None:
    os.environ["LOCALAPPDATA"] = app_data
    module = _load_codex_relay()
    lock = module._binding_lock_for_opaque(module._opaque_thread_id(TARGET_THREAD_ID))
    if not lock.acquire(timeout=5):
        os._exit(2)
    Path(signal_path).write_text("locked", encoding="utf-8")
    os._exit(0)


def _wait_for_signal(path: Path, process: multiprocessing.Process) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists() and path.read_text(encoding="utf-8") == "locked":
            return
        if not process.is_alive():
            break
        time.sleep(0.02)
    raise AssertionError(f"child did not acquire lock; exitcode={process.exitcode}")


class ProcessSharedLockRegressionTests(unittest.TestCase):
    def _lock(self):
        return codex._binding_lock_for_opaque(codex._opaque_thread_id(TARGET_THREAD_ID))

    def test_blocking_waiter_waits_then_acquires(self) -> None:
        with tempfile.TemporaryDirectory() as app_data:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                signal = Path(app_data) / "holder.signal"
                child = multiprocessing.Process(
                    target=_child_hold_lock, args=(app_data, str(signal), 0.45)
                )
                child.start()
                _wait_for_signal(signal, child)
                lock = self._lock()
                started = time.monotonic()
                self.assertTrue(lock.acquire(timeout=2))
                elapsed = time.monotonic() - started
                lock.release()
                child.join(5)
                self.assertEqual(child.exitcode, 0)
                self.assertGreaterEqual(elapsed, 0.20)

    def test_finite_timeout_covers_process_wait(self) -> None:
        with tempfile.TemporaryDirectory() as app_data:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                signal = Path(app_data) / "holder.signal"
                child = multiprocessing.Process(
                    target=_child_hold_lock, args=(app_data, str(signal), 0.8)
                )
                child.start()
                _wait_for_signal(signal, child)
                lock = self._lock()
                started = time.monotonic()
                self.assertFalse(lock.acquire(timeout=0.2))
                elapsed = time.monotonic() - started
                child.join(5)
                self.assertEqual(child.exitcode, 0)
                self.assertGreaterEqual(elapsed, 0.15)
                self.assertLess(elapsed, 0.65)

    def test_process_exit_releases_held_lock(self) -> None:
        with tempfile.TemporaryDirectory() as app_data:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                signal = Path(app_data) / "holder.signal"
                child = multiprocessing.Process(
                    target=_child_exit_while_holding, args=(app_data, str(signal))
                )
                child.start()
                _wait_for_signal(signal, child)
                child.join(5)
                self.assertEqual(child.exitcode, 0)
                lock = self._lock()
                self.assertTrue(lock.acquire(timeout=1))
                lock.release()

    def test_os_acquire_failure_closes_fd_and_releases_thread_lock(self) -> None:
        with tempfile.TemporaryDirectory() as app_data:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                lock = self._lock()
                with mock.patch.object(lock, "_try_os_lock", side_effect=OSError("injected")):
                    with self.assertRaisesRegex(OSError, "injected"):
                        lock.acquire(timeout=0.1)
                self.assertEqual(lock._count, 0)
                self.assertIsNone(lock._fd)
                self.assertTrue(lock._thread_lock.acquire(blocking=False))
                lock._thread_lock.release()

    def test_thread_wait_uses_same_finite_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as app_data:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                lock = self._lock()
                entered = threading.Event()
                release = threading.Event()

                def holder() -> None:
                    with lock:
                        entered.set()
                        release.wait(2)

                thread = threading.Thread(target=holder)
                thread.start()
                self.assertTrue(entered.wait(1))
                started = time.monotonic()
                self.assertFalse(lock.acquire(timeout=0.2))
                elapsed = time.monotonic() - started
                release.set()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertGreaterEqual(elapsed, 0.15)
                self.assertLess(elapsed, 0.65)

    def test_non_owner_cannot_release_os_lock(self) -> None:
        with tempfile.TemporaryDirectory() as app_data:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": app_data}):
                lock = self._lock()
                self.assertTrue(lock.acquire(timeout=1))
                errors = []

                def wrong_owner() -> None:
                    try:
                        lock.release()
                    except RuntimeError as error:
                        errors.append(str(error))

                thread = threading.Thread(target=wrong_owner)
                thread.start()
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(errors), 1)
                self.assertEqual(lock._count, 1)
                self.assertIsNotNone(lock._fd)
                lock.release()
                self.assertEqual(lock._count, 0)
                self.assertIsNone(lock._fd)


if __name__ == "__main__":
    unittest.main()
