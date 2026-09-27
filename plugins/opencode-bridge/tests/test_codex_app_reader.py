from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "codex_app_reader.py"
SPEC = importlib.util.spec_from_file_location("codex_app_reader_test", SCRIPT)
assert SPEC and SPEC.loader
reader = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reader
SPEC.loader.exec_module(reader)


THREAD_ID = "019fd3ed-cb16-7362-af22-cf341aa706d5"
BASELINE_TURN = "019fecdb-6d14-7972-a192-a56c38193837"
NEW_TURN = "019fecdb-6d14-7972-a192-a56c38193838"


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        return None

    def request(self, method, params, *, timeout=None):
        self.calls.append((method, params, timeout))
        if method in {"thread/resume", "thread/start", "turn/start"}:
            raise AssertionError(f"writer method is forbidden: {method}")
        if not self.responses:
            raise AssertionError(f"unexpected request: {method}")
        expected_method, response = self.responses.pop(0)
        if method != expected_method:
            raise AssertionError(f"expected {expected_method}, got {method}")
        return response


def factory_for(client):
    return lambda: client


class CodexAppReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rollout_patch = mock.patch.object(
            reader,
            "_find_rollout_path",
            return_value=Path("C:/fake-rollout.jsonl"),
        )
        self.rollout_patch.start()

    def tearDown(self) -> None:
        self.rollout_patch.stop()

    def test_capture_anchor_keeps_hashes_and_omits_reasoning(self) -> None:
        turn = {
            "id": BASELINE_TURN,
            "status": "completed",
            "items": [
                {"id": "u1", "type": "userMessage", "content": []},
                {"id": "r1", "type": "reasoning", "summary": "private"},
                {"id": "a1", "type": "agentMessage", "text": "done"},
            ],
        }
        client = FakeClient(
            [
                ("thread/read", {"thread": {"id": THREAD_ID}}),
                ("thread/turns/list", {"data": [turn], "nextCursor": None}),
            ]
        )
        anchor = reader.capture_thread_anchor(
            THREAD_ID,
            client_factory=factory_for(client),
        )
        self.assertEqual(anchor["turn_id"], BASELINE_TURN)
        self.assertEqual(anchor["turn_status"], "completed")
        self.assertEqual(set(anchor["item_hashes"]), {"u1", "r1", "a1"})
        self.assertEqual([item["id"] for item in anchor["visible_items"]], ["u1", "a1"])
        self.assertEqual(anchor["omitted_items"], 1)

    def test_reader_uses_observer_methods_only(self) -> None:
        turn = {
            "id": BASELINE_TURN,
            "status": "completed",
            "items": [{"id": "a1", "type": "agentMessage", "text": "done"}],
        }
        client = FakeClient(
            [
                ("thread/read", {"thread": {"id": THREAD_ID}}),
                ("thread/turns/list", {"data": [turn], "nextCursor": None}),
            ]
        )

        anchor = reader.capture_thread_anchor(
            THREAD_ID,
            client_factory=factory_for(client),
        )

        self.assertEqual(anchor["turn_id"], BASELINE_TURN)
        self.assertEqual(
            client.calls,
            [
                ("thread/read", {"threadId": THREAD_ID, "includeTurns": False}, None),
                (
                    "thread/turns/list",
                    {
                        "threadId": THREAD_ID,
                        "limit": 1,
                        "sortDirection": "desc",
                        "itemsView": "full",
                    },
                    None,
                ),
            ],
        )

    def test_read_delta_returns_new_turn_and_changed_baseline_item(self) -> None:
        original_agent = {"id": "a1", "type": "agentMessage", "text": "working"}
        changed_agent = {"id": "a1", "type": "agentMessage", "text": "finished"}
        baseline = {
            "id": BASELINE_TURN,
            "status": "completed",
            "startedAt": 1,
            "completedAt": 2,
            "items": [changed_agent],
        }
        new_turn = {
            "id": NEW_TURN,
            "status": "completed",
            "startedAt": 3,
            "completedAt": 4,
            "items": [
                {"id": "u2", "type": "userMessage", "content": []},
                {"id": "a2", "type": "agentMessage", "text": "new result"},
            ],
        }
        client = FakeClient(
            [
                (
                    "thread/read",
                    {
                        "thread": {
                            "id": THREAD_ID,
                            "title": "Target",
                            "status": {"type": "idle"},
                        }
                    },
                ),
                (
                    "thread/turns/list",
                    {"data": [new_turn, baseline], "nextCursor": None},
                ),
            ]
        )
        delta = reader.read_thread_delta(
            THREAD_ID,
            {
                "turn_id": BASELINE_TURN,
                "item_hashes": {"a1": reader._item_hash(original_agent)},
            },
            client_factory=factory_for(client),
        )
        self.assertEqual(delta["turn_count"], 2)
        self.assertTrue(delta["turns"][0]["continued_baseline_turn"])
        self.assertEqual(delta["turns"][0]["items"][0]["text"], "finished")
        self.assertFalse(delta["turns"][1]["continued_baseline_turn"])
        self.assertEqual(delta["turns"][1]["items"][1]["text"], "new result")
        self.assertEqual(delta["thread"]["status"], {"type": "idle"})

    def test_missing_baseline_fails_closed(self) -> None:
        client = FakeClient(
            [
                ("thread/read", {"thread": {"id": THREAD_ID}}),
                ("thread/turns/list", {"data": [], "nextCursor": None}),
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "baseline was not found"):
            reader.read_thread_delta(
                THREAD_ID,
                {"turn_id": BASELINE_TURN, "item_hashes": {}},
                client_factory=factory_for(client),
            )


if __name__ == "__main__":
    unittest.main()
