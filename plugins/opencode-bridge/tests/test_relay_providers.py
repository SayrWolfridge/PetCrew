from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import importlib
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import relay_providers as providers

SOURCE = "019fd933-87b0-7d93-9666-bf02bf4f03d5"
TARGET = "019fd3ed-cb16-7362-af22-cf341aa706d5"
OTHER = "019fe6a7-09e3-75f0-a368-4eb18afd746e"


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = mock.patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        # Other test modules may reload provider modules under their canonical name.
        self.codex = importlib.import_module("codex_relay")
        self.opencode = importlib.import_module("opencode_relay")
        self.anchor = mock.patch.object(self.codex, "capture_thread_anchor",
                                      return_value={"turn_id": None, "item_hashes": {}})
        self.anchor.start()
        self.addCleanup(self.anchor.stop)
        self.cursor = mock.patch.object(self.opencode, "_read_cursor", return_value=0)
        self.cursor.start()
        self.addCleanup(self.cursor.stop)

    def register(self, provider, source=SOURCE, transfer=False):
        if provider == "codex":
            return self.codex.register_binding(TARGET, source, self.temp.name,
                "cursor-before-send", 0, {"turn_id": None, "item_hashes": {}},
                transfer_parent=transfer)[0]
        return self.opencode.register_binding("ses_provider_test", self.temp.name,
                                              source, transfer_parent=transfer)[0]

    def event(self, provider, binding):
        return dict(provider=provider, cursor=1, completion_id="completion:" + "a" * 64,
                    session_id=binding["opaque_target_session_id" if provider == "codex"
                                       else "opaque_session_id"],
                    agent_id="turn:" + "b" * 64, phase="completed",
                    completed_at=datetime.now(timezone.utc).isoformat())

    def job(self, provider):
        binding = self.register(provider)
        event = self.event(provider, binding)
        fields = providers.lookup_event(event)
        self.assertIsNotNone(fields)
        return SimpleNamespace(**fields), binding, event

    def test_exact_prompt_and_idempotent_attempted_and_delivered_recovery(self):
        for provider in ("codex", "opencode"):
            with self.subTest(provider=provider):
                job, binding, event = self.job(provider)
                self.assertEqual(job.source_task_id, SOURCE)
                self.assertEqual(job.codex_thread_id, TARGET if provider == "codex" else SOURCE)
                expected = (self.codex._resume_prompt(binding, event) if provider == "codex"
                            else self.opencode._resume_prompt(binding, "completed", job.completion_id))
                self.assertEqual(providers.prompt(job), expected)
                self.assertIn(job.completion_id, expected)
                self.assertTrue(providers.validate(job))
                self.assertFalse(providers.complete(job))
                self.assertTrue(providers.claim(job))
                self.assertTrue(providers.claim(job))
                self.assertTrue(providers.validate(job))
                self.assertIsNone(providers.lookup_event(event))
                self.assertEqual(providers.prompt(job), expected)
                self.assertTrue(providers.complete(job))
                self.assertTrue(providers.complete(job))
                self.assertTrue(providers.claim(job))

    def test_delivered_without_wal_cannot_be_admitted_as_new_return(self):
        for provider in ("codex", "opencode"):
            with self.subTest(provider=provider):
                job, _, event = self.job(provider)
                self.assertTrue(providers.claim(job))
                self.assertTrue(providers.complete(job))
                # No journal was created: this is also the post-retention state.
                self.assertIsNone(providers.lookup_event(event))
                self.assertTrue(providers.complete(job))

    def test_attempted_without_wal_is_ambiguous_not_fresh_admission(self):
        for provider in ("codex", "opencode"):
            with self.subTest(provider=provider):
                job, _, event = self.job(provider)
                self.assertTrue(providers.claim(job))
                self.assertIsNone(providers.lookup_event(event))
                # If an exact WAL exists, its callbacks still recover idempotently.
                self.assertTrue(providers.validate(job))
                self.assertTrue(providers.claim(job))

    def assert_rejected(self, job):
        self.assertFalse(providers.validate(job))
        self.assertFalse(providers.claim(job))
        self.assertFalse(providers.complete(job))
        with self.assertRaises(ValueError):
            providers.prompt(job)

    def test_transfer_rejects_old_job_even_when_claimed(self):
        for provider in ("codex", "opencode"):
            with self.subTest(provider=provider):
                job, _, _ = self.job(provider)
                self.assertTrue(providers.claim(job))
                self.register(provider, source=OTHER, transfer=True)
                self.assert_rejected(job)

    def test_detach_rejects_old_job(self):
        for provider in ("codex", "opencode"):
            with self.subTest(provider=provider):
                job, _, _ = self.job(provider)
                if provider == "codex":
                    self.codex.detach_binding(TARGET, SOURCE)
                else:
                    self.opencode.detach_binding("ses_provider_test", SOURCE)
                self.assert_rejected(job)

    def test_source_target_workspace_generation_are_independently_checked(self):
        for provider in ("codex", "opencode"):
            job, _, _ = self.job(provider)
            for field, value in (("source_task_id", OTHER), ("codex_thread_id", OTHER),
                                 ("workspace", self.temp.name + "-other"),
                                 ("binding_generation", "c" * 32)):
                with self.subTest(provider=provider, field=field):
                    bad = SimpleNamespace(**{**vars(job), field: value})
                    self.assert_rejected(bad)

    def test_consumed_codex_accepts_only_exact_receipt(self):
        job, _, event = self.job("codex")
        self.assertTrue(providers.claim(job))
        for field, value in (("completion_id", "completion:" + "d" * 64),
                             ("cursor", 2), ("phase", "failed")):
            with self.subTest(field=field):
                self.assertIsNone(providers.lookup_event({**event, field: value}))

    def test_malformed_or_prebinding_events_rejected(self):
        for provider in ("codex", "opencode"):
            _, _, event = self.job(provider)
            for field, value in (("cursor", True), ("phase", "working"),
                                 ("session_id", "../../anything"),
                                 ("completion_id", "arbitrary"),
                                 ("completed_at", "2000-01-01T00:00:00+00:00")):
                with self.subTest(provider=provider, field=field):
                    self.assertIsNone(providers.lookup_event({**event, field: value}))
        self.assertIsNone(providers.lookup_event({}))

    def test_source_terminal_matches_hash_without_binding_scan(self):
        event = dict(provider="codex", cursor=22, completion_id="completion:" + "e" * 64,
                     session_id=self.codex._opaque_thread_id(SOURCE),
                     agent_id="turn:" + "f" * 64, phase="completed",
                     completed_at=datetime.now(timezone.utc).isoformat())
        with mock.patch.object(self.codex, "_read_binding", side_effect=AssertionError("scan")):
            self.assertTrue(providers.source_matches_event(SOURCE, event))
            self.assertFalse(providers.source_matches_event(OTHER, event))
            self.assertFalse(providers.source_matches_event(SOURCE, {**event, "phase": "working"}))
            self.assertFalse(providers.source_matches_event(SOURCE, {**event, "agent_id": ""}))


if __name__ == "__main__":
    unittest.main()
