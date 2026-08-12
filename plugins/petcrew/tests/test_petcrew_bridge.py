from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest


BRIDGE_PATH = Path(__file__).parents[1] / "scripts" / "petcrew_bridge.py"
SPEC = importlib.util.spec_from_file_location("petcrew_bridge", BRIDGE_PATH)
assert SPEC is not None and SPEC.loader is not None
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class PetCrewBridgeTests(unittest.TestCase):
    def test_hook_never_transmits_prompt_or_tool_payload(self) -> None:
        secret = "synthetic-sensitive-value"
        prompt_event = bridge.event_from_hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "prompt": f"private {secret}",
            }
        )
        tool_event = bridge.event_from_hook(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "tool_name": "Bash",
                "tool_input": {"command": f"echo {secret}"},
            }
        )
        serialized = json.dumps([prompt_event, tool_event], ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("echo", serialized)
        self.assertIsNone(tool_event)

    def test_subagent_requires_stable_id_and_keeps_parent(self) -> None:
        base = {
            "hook_event_name": "SubagentStart",
            "session_id": "session-1",
            "turn_id": "turn-1",
        }
        self.assertIsNone(bridge.event_from_hook(base))
        event = bridge.event_from_hook({**base, "agent_id": "child-7"})
        self.assertTrue(event["agent_id"].startswith("agent:"))
        self.assertTrue(event["parent_agent_id"].startswith("turn:"))
        self.assertNotIn("child-7", event["agent_id"])
        self.assertNotIn("turn-1", event["parent_agent_id"])

    def test_request_user_input_is_attention_not_generic_activity(self) -> None:
        event = bridge.event_from_hook(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "tool_name": "request_user_input",
            }
        )

        self.assertEqual(event["event_type"], "agent.attention_requested")
        self.assertEqual(event["payload"]["phase"], "waiting_input")
        self.assertEqual(event["payload"]["attention"]["kind"], "input")
        self.assertEqual(event["payload"]["current_action"], "Ждёт выбора")

    def test_hook_hashes_identity_and_keeps_only_project_name(self) -> None:
        event = bridge.event_from_hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "private-session",
                "turn_id": "private-turn",
                "cwd": r"C:\\Projects\\SampleProject\\sample_report",
            }
        )
        serialized = json.dumps(event, ensure_ascii=False)
        self.assertNotIn("private-session", serialized)
        self.assertNotIn("private-turn", serialized)
        self.assertNotIn(r"C:\\Projects\\SampleProject", serialized)
        self.assertEqual(event["payload"]["project"]["name"], "sample_report")
        self.assertIsNone(event["payload"]["project"]["path"])

    def test_registry_atomically_keeps_only_latest_event_per_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = {
                "session_id": "session-1",
                "turn_id": "turn-1",
                "cwd": r"C:\\Work\\PetCrew",
            }
            started = bridge.event_from_hook({**base, "hook_event_name": "UserPromptSubmit"})
            self.assertTrue(bridge.persist_registry_event(started, data_dir=root))
            files = list((root / bridge.REGISTRY_FOLDER).glob("*.json"))
            self.assertEqual(len(files), 1)

            waiting = bridge.event_from_hook(
                {
                    **base,
                    "hook_event_name": "PreToolUse",
                    "tool_name": "request_user_input",
                }
            )
            self.assertTrue(bridge.persist_registry_event(waiting, data_dir=root))
            files = list((root / bridge.REGISTRY_FOLDER).glob("*.json"))
            self.assertEqual(len(files), 1)
            persisted = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(persisted["event_id"], waiting["event_id"])
            self.assertEqual(persisted["payload"]["current_action"], "Ждёт выбора")
            self.assertNotIn("session-1", files[0].name)

    def test_request_user_input_output_resumes_working(self) -> None:
        event = bridge.event_from_hook(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "tool_name": "request_user_input",
                "tool_input": {"questions": "PRIVATE"},
                "tool_response": {"answer": "PRIVATE"},
            }
        )

        self.assertEqual(event["event_type"], "agent.activity")
        self.assertEqual(event["payload"]["phase"], "working")
        self.assertEqual(event["payload"]["current_action"], "Продолжает работу")
        self.assertNotIn("PRIVATE", json.dumps(event, ensure_ascii=False))

    def test_explicit_progress_is_preserved(self) -> None:
        event = bridge.event_from_report(
            {
                "status": "progress",
                "task_title": "Проверка файлов",
                "agent_id": "worker-2",
                "parent_agent_id": "root",
                "action": "Проверяет второй пакет",
                "current": 4,
                "total": 10,
            }
        )
        self.assertEqual(event["event_type"], "agent.progress")
        self.assertEqual(event["payload"]["progress"]["current"], 4)
        self.assertEqual(event["payload"]["progress"]["total"], 10)
        self.assertEqual(event["payload"]["progress"]["source"], "explicit")

    def test_update_plan_hook_keeps_only_status_counts(self) -> None:
        event = bridge.event_from_hook(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "tool_name": "update_plan",
                "tool_input": {
                    "explanation": "private explanation",
                    "plan": [
                        {"step": "private completed step", "status": "completed"},
                        {"step": "private active step", "status": "in_progress"},
                    ],
                },
            }
        )
        self.assertEqual(event["event_type"], "agent.progress")
        self.assertEqual(event["payload"]["progress"]["current"], 1)
        self.assertEqual(event["payload"]["progress"]["total"], 2)
        serialized = json.dumps(event, ensure_ascii=False)
        self.assertNotIn("private", serialized)

    def test_partial_or_invalid_progress_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            bridge.event_from_report(
                {"status": "progress", "task_title": "Task", "current": 1}
            )
        with self.assertRaises(ValueError):
            bridge.event_from_report(
                {"status": "progress", "task_title": "Task", "current": 3, "total": 2}
            )

    def test_rpc_lists_and_calls_one_tool(self) -> None:
        listed = bridge.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual([tool["name"] for tool in listed["result"]["tools"]], [bridge.TOOL_NAME])
        captured = []
        called = bridge.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": bridge.TOOL_NAME,
                    "arguments": {"status": "working", "task_title": "Task", "action": "Checks"},
                },
            },
            reporter=lambda event: captured.append(event) is None,
        )
        self.assertTrue(called["result"]["structuredContent"]["delivered"])
        self.assertEqual(len(captured), 1)

    def test_local_hub_discovery_and_authorized_post(self) -> None:
        received: list[tuple[str | None, dict]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length))
                received.append((self.headers.get("Authorization"), body))
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"revision":1}')

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                token = "a" * 64
                secret = root / "hub-secret.txt"
                secret.write_text(token, encoding="utf-8")
                (root / "hub-runtime.json").write_text(
                    json.dumps(
                        {
                            "endpoint": f"http://127.0.0.1:{server.server_port}",
                            "protocol_version": "1.0",
                            "process_id": 1,
                            "secret_file": str(secret),
                        }
                    ),
                    encoding="utf-8",
                )
                event = bridge.event_from_report({"status": "started", "task_title": "Task"})
                self.assertTrue(bridge.post_event(event, data_dir=root))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(received[0][0], f"Bearer {'a' * 64}")
        self.assertEqual(received[0][1]["provider"], "codex")

    def test_remote_runtime_endpoint_is_rejected_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / "hub-secret.txt"
            secret.write_text("a" * 64, encoding="utf-8")
            (root / "hub-runtime.json").write_text(
                json.dumps(
                    {
                        "endpoint": "https://example.com",
                        "protocol_version": "1.0",
                        "secret_file": str(secret),
                    }
                ),
                encoding="utf-8",
            )
            self.assertIsNone(bridge.discover_connection(root))


if __name__ == "__main__":
    unittest.main()
