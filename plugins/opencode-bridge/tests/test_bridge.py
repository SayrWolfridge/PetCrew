from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "opencode_bridge.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("opencode_bridge", SCRIPT)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)


class BridgeTests(unittest.TestCase):
    def test_initialize_and_tools(self) -> None:
        initialized = bridge.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"},
            }
        )
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-03-26")
        listed = bridge.handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {item["name"] for item in listed["result"]["tools"]}
        self.assertEqual(
            names,
            {
                "codex_bind_return",
                "codex_confirm_return",
                "codex_rollback_return",
                "codex_detach_return",
                "codex_return_status",
                "codex_read_return",
                "codex_get_return_parent",
                "codex_transfer_return_parent",
                "opencode_health",
                "opencode_list_sessions",
                "opencode_read_session",
                "opencode_start_task",
                "opencode_continue",
                "opencode_detach",
                "opencode_wait",
                "opencode_answer_question",
                "opencode_reply_permission",
                "opencode_abort",
            },
        )
        tools = {item["name"]: item for item in listed["result"]["tools"]}
        for name in {
            "opencode_health",
            "opencode_list_sessions",
            "opencode_read_session",
            "opencode_wait",
            "codex_return_status",
            "codex_read_return",
        }:
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"])
        self.assertNotIn("annotations", tools["opencode_start_task"])
        start_schema = tools["opencode_start_task"]["inputSchema"]
        self.assertEqual(start_schema["properties"]["model"]["type"], "string")
        self.assertNotIn("model", start_schema["required"])
        self.assertNotIn("model", tools["opencode_continue"]["inputSchema"]["properties"])
        self.assertIn(
            "codex_thread_id",
            tools["opencode_start_task"]["inputSchema"]["required"],
        )
        self.assertIn(
            "codex_thread_id",
            tools["opencode_continue"]["inputSchema"]["required"],
        )
        self.assertIn(
            "transfer_parent",
            tools["opencode_continue"]["inputSchema"]["properties"],
        )
        self.assertIn(
            "target_cursor",
            tools["codex_bind_return"]["inputSchema"]["required"],
        )

    def test_workspace_must_exist_and_be_absolute(self) -> None:
        with self.assertRaises(ValueError):
            bridge._validate_workspace("relative")
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(bridge._validate_workspace(folder), str(Path(folder).resolve()))

    def test_submission_rejects_empty_git_markers_before_mutation(self) -> None:
        thread_id = "019f9a58-1f22-7f63-bd1c-7c480dd3ea1d"
        for marker_kind in ("directory", "file"):
            with self.subTest(marker_kind=marker_kind), tempfile.TemporaryDirectory() as folder:
                marker = Path(folder) / ".git"
                if marker_kind == "directory":
                    marker.mkdir()
                else:
                    marker.touch()
                with mock.patch.object(bridge, "_request") as request, mock.patch.object(
                    bridge, "register_binding"
                ) as register:
                    with self.assertRaisesRegex(
                        RuntimeError, r"empty \.git (directory|file).*not submitted"
                    ):
                        bridge.call_tool(
                            "opencode_continue",
                            {
                                "workspace": folder,
                                "session_id": "ses_test",
                                "prompt": "Continue",
                                "codex_thread_id": thread_id,
                            },
                        )
                request.assert_not_called()
                register.assert_not_called()

    def test_start_task_rejects_empty_git_before_session_creation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / ".git").mkdir()
            with mock.patch.object(bridge, "_request") as request, mock.patch.object(
                bridge, "register_binding"
            ) as register:
                with self.assertRaisesRegex(RuntimeError, "task was not submitted"):
                    bridge.call_tool(
                        "opencode_start_task",
                        {
                            "workspace": folder,
                            "prompt": "Inspect only",
                            "codex_thread_id": "019f9a58-1f22-7f63-bd1c-7c480dd3ea1d",
                        },
                    )
            request.assert_not_called()
            register.assert_not_called()

    def test_codex_return_binding_captures_petcrew_baseline_before_send(self) -> None:
        binding = {
            "target_codex_thread_id": "019fd3ed-cb16-7362-af22-cf341aa706d5",
            "source_codex_thread_id": "019fd933-87b0-7d93-9666-bf02bf4f03d5",
            "target_cursor": "fresh-target-cursor",
            "binding_id": "b" * 32,
            "after_cursor": 41,
            "return_state": "prepared",
            "baseline_anchor": {
                "turn_id": "019fecdb-6d14-7972-a192-a56c38193837",
                "item_hashes": {"item": "a" * 64},
            },
        }
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            bridge, "_petcrew_inbox", return_value={"latest_cursor": 41}
        ), mock.patch.object(
            bridge,
            "capture_thread_anchor",
            return_value={
                **binding["baseline_anchor"],
                "turn_status": "completed",
                "visible_items": [{"id": "item", "type": "agentMessage", "text": "before"}],
                "omitted_items": 0,
            },
        ), mock.patch.object(
            bridge,
            "register_codex_binding",
            return_value=(binding, True, "c" * 64),
        ) as register:
            result = bridge.call_tool(
                "codex_bind_return",
                {
                    "workspace": folder,
                    "target_thread_id": binding["target_codex_thread_id"],
                    "source_thread_id": binding["source_codex_thread_id"],
                    "target_cursor": binding["target_cursor"],
                },
            )
        payload = result["structuredContent"]
        self.assertEqual(payload["state"], "bound")
        self.assertEqual(payload["baseline_completion_cursor"], 41)
        self.assertEqual(payload["rollback_token"], "c" * 64)
        self.assertEqual(payload["baseline_turn_id"], binding["baseline_anchor"]["turn_id"])
        self.assertEqual(payload["baseline_visible_items"][0]["text"], "before")
        self.assertTrue(payload["persistent_parent_binding"])
        self.assertTrue(payload["one_shot_return"])
        self.assertEqual(payload["return_state"], "prepared")
        register.assert_called_once_with(
            binding["target_codex_thread_id"],
            binding["source_codex_thread_id"],
            str(Path(folder).resolve()),
            binding["target_cursor"],
            41,
            binding["baseline_anchor"],
            transfer_parent=False,
        )

    def test_codex_return_status_exposes_in_flight_and_runtime_provenance(self) -> None:
        target_thread_id = "019fd3ed-cb16-7362-af22-cf341aa706d5"
        report = {
            "state": "bound",
            "diagnostic": None,
            "runtime": {
                "plugin_version": "test",
                "runtime_root": "C:\\plugin",
                "runtime_exists": True,
                "runtime_stale": False,
                "relay_state_path": "C:\\relay",
                "binding_schema_versions": [1, 2, 3],
            },
            "in_flight_completion_ids": ["completion:" + "a" * 64],
            "binding": {
                "binding_id": "b" * 32,
                "target_codex_thread_id": target_thread_id,
                "source_codex_thread_id": "019fd933-87b0-7d93-9666-bf02bf4f03d5",
                "workspace": "C:\\workspace",
                "target_cursor": "cursor",
                "bound_at": "2026-08-10T20:20:10+00:00",
                "after_cursor": 104,
                "last_attempted_cursor": 112,
                "attempted_completion_ids": ["completion:" + "a" * 64],
                "delivered_completion_ids": [],
                "baseline_anchor": {"turn_id": None, "item_hashes": {}},
                "pending_confirmation": False,
                "return_state": "consumed",
            },
        }
        with tempfile.TemporaryDirectory() as workspace, mock.patch.object(
            bridge, "codex_binding_status_report", return_value=report
        ):
            result = bridge.call_tool(
                "codex_return_status",
                {"workspace": workspace, "target_thread_id": target_thread_id},
            )
        payload = result["structuredContent"]
        self.assertEqual(payload["state"], "bound")
        self.assertTrue(payload["bound"])
        self.assertEqual(payload["binding"]["in_flight_completion_count"], 1)
        self.assertFalse(payload["binding"]["return_armed"])
        self.assertTrue(payload["binding"]["one_shot_return"])
        self.assertEqual(payload["runtime"]["plugin_version"], "test")

    def test_codex_return_status_does_not_report_invalid_state_as_unbound(self) -> None:
        target_thread_id = "019fd3ed-cb16-7362-af22-cf341aa706d5"
        with tempfile.TemporaryDirectory() as workspace, mock.patch.object(
            bridge,
            "codex_binding_status_report",
            return_value={
                "state": "state_invalid",
                "binding": None,
                "diagnostic": "binding schema validation failed",
                "runtime": {"runtime_stale": False},
            },
        ):
            result = bridge.call_tool(
                "codex_return_status",
                {"workspace": workspace, "target_thread_id": target_thread_id},
            )
        payload = result["structuredContent"]
        self.assertEqual(payload["state"], "state_invalid")
        self.assertIsNone(payload["bound"])
        self.assertIsNone(payload["binding"])

    def test_codex_return_status_does_not_trust_binding_from_stale_runtime(self) -> None:
        target_thread_id = "019fd3ed-cb16-7362-af22-cf341aa706d5"
        with tempfile.TemporaryDirectory() as workspace, mock.patch.object(
            bridge,
            "codex_binding_status_report",
            return_value={
                "state": "runtime_stale",
                "binding": {
                    "binding_id": "b" * 32,
                    "target_codex_thread_id": target_thread_id,
                    "source_codex_thread_id": "019fd933-87b0-7d93-9666-bf02bf4f03d5",
                    "workspace": "C:\\workspace",
                    "target_cursor": "cursor",
                    "bound_at": "2026-08-10T20:20:10+00:00",
                    "after_cursor": 104,
                    "last_attempted_cursor": 112,
                    "attempted_completion_ids": [],
                    "delivered_completion_ids": [],
                    "baseline_anchor": {"turn_id": None, "item_hashes": {}},
                    "pending_confirmation": False,
                    "return_state": "consumed",
                },
                "in_flight_completion_ids": [],
                "diagnostic": "runtime files are no longer available",
                "runtime": {"runtime_stale": True},
            },
        ):
            result = bridge.call_tool(
                "codex_return_status",
                {"workspace": workspace, "target_thread_id": target_thread_id},
            )
        payload = result["structuredContent"]
        self.assertEqual(payload["state"], "runtime_stale")
        self.assertIsNone(payload["bound"])
        self.assertIsNotNone(payload["binding"])

    def test_codex_read_return_verifies_receipt_and_reads_exact_delta(self) -> None:
        target_thread_id = "019fd3ed-cb16-7362-af22-cf341aa706d5"
        source_thread_id = "019fd933-87b0-7d93-9666-bf02bf4f03d5"
        completion_id = "completion:" + "a" * 64
        anchor = {
            "turn_id": "019fecdb-6d14-7972-a192-a56c38193837",
            "item_hashes": {"item": "b" * 64},
        }
        delta = {
            "target_thread_id": target_thread_id,
            "turn_count": 1,
            "turns": [{"turn_id": "new", "items": []}],
        }
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            bridge,
            "codex_return_receipt",
            return_value={
                "phase": "completed",
                "receipt_state": "attempted_current_resume",
                "anchor": anchor,
            },
        ) as receipt, mock.patch.object(
            bridge,
            "read_thread_delta",
            return_value=delta,
        ) as read_delta:
            result = bridge.call_tool(
                "codex_read_return",
                {
                    "workspace": folder,
                    "target_thread_id": target_thread_id,
                    "source_thread_id": source_thread_id,
                    "completion_id": completion_id,
                },
            )
        payload = result["structuredContent"]
        self.assertTrue(payload["matching_terminal_receipt"])
        self.assertEqual(payload["receipt_state"], "attempted_current_resume")
        self.assertEqual(payload["delta"], delta)
        receipt.assert_called_once_with(
            target_thread_id,
            source_thread_id,
            str(Path(folder).resolve()),
            completion_id,
        )
        read_delta.assert_called_once_with(target_thread_id, anchor)

    def test_codex_reader_http_timeout_exceeds_long_resume_bound(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = b'{"result":{}}'
        response.__enter__.return_value = response
        response.__exit__.return_value = None
        with mock.patch.object(bridge, "ensure_server"), mock.patch.object(
            bridge,
            "_auth_header",
            return_value="Basic test",
        ), mock.patch.object(
            bridge.urllib.request,
            "urlopen",
            return_value=response,
        ) as urlopen:
            result = bridge._codex_reader_request(
                "/v1/codex/delta",
                {"thread_id": "019fd3ed-cb16-7362-af22-cf341aa706d5"},
            )

        self.assertEqual(result, {})
        self.assertEqual(
            urlopen.call_args.kwargs["timeout"],
            bridge.CODEX_READER_HTTP_TIMEOUT_SECONDS,
        )
        self.assertGreater(bridge.CODEX_READER_HTTP_TIMEOUT_SECONDS, 60.0)

    def test_codex_binding_confirmation_accepts_relay_auto_confirm(self) -> None:
        target_thread_id = "019fd3ed-cb16-7362-af22-cf341aa706d5"
        binding_id = "b" * 32
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            bridge, "confirm_codex_binding", return_value=False
        ), mock.patch.object(
            bridge,
            "codex_binding_status",
            return_value={
                "binding_id": binding_id,
                "pending_confirmation": False,
                "return_state": "consumed",
            },
        ):
            result = bridge.call_tool(
                "codex_confirm_return",
                {
                    "workspace": folder,
                    "target_thread_id": target_thread_id,
                    "binding_id": binding_id,
                    "rollback_token": "c" * 64,
                },
            )
        self.assertTrue(result["structuredContent"]["confirmed"])
        self.assertTrue(result["structuredContent"]["already_confirmed"])
        self.assertEqual(result["structuredContent"]["return_state"], "consumed")
        self.assertFalse(result["structuredContent"]["return_armed"])

    @unittest.skipUnless(os.name == "nt", "Windows npm launcher behavior")
    def test_find_opencode_prefers_native_npm_binary(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launcher = root / "opencode.CMD"
            native = root / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
            native.parent.mkdir(parents=True)
            launcher.touch()
            native.touch()
            with mock.patch.object(bridge.shutil, "which", return_value=str(launcher)):
                self.assertEqual(bridge._find_opencode(), str(native))

    def test_start_task_defaults_to_plan(self) -> None:
        calls = []

        def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if method == "POST" and path == "/session":
                return {"id": "ses_test"}
            return None

        binding = {"binding_id": "binding-test"}
        registration = (binding, True, None)
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            bridge, "_request", side_effect=fake_request
        ), mock.patch.object(
            bridge, "register_binding", return_value=registration
        ) as register:
            result = bridge.call_tool(
                "opencode_start_task",
                {
                    "workspace": folder,
                    "prompt": "Inspect only",
                    "codex_thread_id": "019f9a58-1f22-7f63-bd1c-7c480dd3ea1d",
                },
            )
        self.assertFalse(result["isError"])
        self.assertEqual(calls[0][2]["body"]["agent"], "plan")
        self.assertEqual(calls[1][1], "/session/ses_test/prompt_async")
        self.assertEqual(calls[1][2]["body"]["agent"], "plan")
        self.assertEqual(
            calls[1][2]["body"]["model"],
            {"providerID": "opencode", "modelID": "mimo-v2.6-flash-free"},
        )
        self.assertEqual(
            result["structuredContent"]["model"],
            "opencode/mimo-v2.6-flash-free",
        )
        self.assertTrue(result["structuredContent"]["wake_on_completion"])
        self.assertTrue(result["structuredContent"]["persistent_parent_binding"])
        register.assert_called_once_with(
            "ses_test",
            str(Path(folder).resolve()),
            "019f9a58-1f22-7f63-bd1c-7c480dd3ea1d",
        )

    def test_start_task_uses_explicit_provider_and_nested_model_id(self) -> None:
        for model in ("other-provider/model", "test-provider/family/model", "opencode/mimo-v2.6-flash-free"):
            with self.subTest(model=model), tempfile.TemporaryDirectory() as folder, mock.patch.object(
                bridge, "_request", side_effect=[{"id": "ses_test"}, None]
            ) as request, mock.patch.object(
                bridge, "register_binding", return_value=({"binding_id": "test-binding"}, True, None)
            ) as register:
                result = bridge.call_tool(
                    "opencode_start_task",
                    {
                        "workspace": folder,
                        "prompt": "Inspect only",
                        "codex_thread_id": "019f9a58-1f22-7f63-bd1c-7c480dd3ea1d",
                        "model": model,
                    },
                )
            provider_id, model_id = model.split("/", 1)
            self.assertEqual(
                request.call_args.kwargs["body"]["model"],
                {"providerID": provider_id, "modelID": model_id},
            )
            self.assertEqual(result["structuredContent"]["model"], model)
            self.assertTrue(result["structuredContent"]["persistent_parent_binding"])
            register.assert_called_once()
        self.assertEqual(bridge.DEFAULT_START_MODEL, {"providerID": "opencode", "modelID": "mimo-v2.6-flash-free"})

    def test_invalid_model_fails_before_any_request_or_binding(self) -> None:
        invalid = (None, True, 42, {}, [], "", "model-only", "/model", "provider/",
                   "provider//model", "provider/model/", " provider/model", "provider/model ",
                   "provider/my model", "provider/model\n", "provider/\tmodel")
        with tempfile.TemporaryDirectory() as folder:
            for model in invalid:
                with self.subTest(model=model), mock.patch.object(bridge, "_request") as request, mock.patch.object(
                    bridge, "register_binding"
                ) as register:
                    response = bridge.handle_rpc(
                        {
                            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {
                                "name": "opencode_start_task",
                                "arguments": {
                                    "workspace": folder, "prompt": "Inspect only",
                                    "codex_thread_id": "019f9a58-1f22-7f63-bd1c-7c480dd3ea1d",
                                    "model": model,
                                },
                            },
                        }
                    )
                self.assertTrue(response["result"]["isError"])
                self.assertIn("provider/model", response["result"]["structuredContent"]["error"])
                request.assert_not_called()
                register.assert_not_called()

    def test_explicit_model_submission_failure_rolls_back_without_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            bridge, "_request", side_effect=[{"id": "ses_test"}, RuntimeError("provider rejected"), None]
        ) as request, mock.patch.object(
            bridge, "register_binding", return_value=({"binding_id": "test-binding"}, True, None)
        ), mock.patch.object(bridge, "rollback_binding") as rollback:
            with self.assertRaisesRegex(RuntimeError, "provider rejected"):
                bridge.call_tool(
                    "opencode_start_task",
                    {
                        "workspace": folder, "prompt": "Inspect only",
                        "codex_thread_id": "019f9a58-1f22-7f63-bd1c-7c480dd3ea1d",
                        "model": "test-provider/model",
                    },
                )
        self.assertEqual([(c.args[0], c.args[1]) for c in request.call_args_list], [
            ("POST", "/session"), ("POST", "/session/ses_test/prompt_async"), ("DELETE", "/session/ses_test")
        ])
        self.assertEqual(request.call_args_list[1].kwargs["body"]["model"], {"providerID": "test-provider", "modelID": "model"})
        rollback.assert_called_once_with("ses_test", "test-binding", None)

    def test_continue_rejects_model_override_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(bridge, "_request") as request, mock.patch.object(
            bridge, "register_binding"
        ) as register:
            with self.assertRaisesRegex(ValueError, "preserves the existing session model"):
                bridge.call_tool(
                    "opencode_continue",
                    {
                        "workspace": folder, "session_id": "ses_test", "prompt": "Continue",
                        "codex_thread_id": "019f9a58-1f22-7f63-bd1c-7c480dd3ea1d",
                        "model": "test-provider/model",
                    },
                )
        request.assert_not_called()
        register.assert_not_called()

    def test_start_task_without_thread_id_fails_before_creating_session(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            bridge, "_request"
        ) as request:
            with self.assertRaisesRegex(ValueError, "codex_thread_id is required"):
                bridge.call_tool(
                    "opencode_start_task",
                    {"workspace": folder, "prompt": "Inspect only"},
                )
        request.assert_not_called()

    def test_start_task_does_not_submit_when_route_cannot_be_armed(self) -> None:
        calls = []

        def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if method == "POST" and path == "/session":
                return {"id": "ses_test"}
            return None

        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            bridge, "_request", side_effect=fake_request
        ), mock.patch.object(bridge, "register_binding", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "binding was not created"):
                bridge.call_tool(
                    "opencode_start_task",
                    {
                        "workspace": folder,
                        "prompt": "Inspect only",
                        "codex_thread_id": "019f9a58-1f22-7f63-bd1c-7c480dd3ea1d",
                    },
                )
        self.assertEqual(
            [(method, path) for method, path, _kwargs in calls],
            [("POST", "/session"), ("DELETE", "/session/ses_test")],
        )

    def test_continue_without_thread_id_fails_before_submitting_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            bridge, "_request"
        ) as request:
            with self.assertRaisesRegex(ValueError, "codex_thread_id is required"):
                bridge.call_tool(
                    "opencode_continue",
                    {
                        "workspace": folder,
                        "session_id": "ses_test",
                        "prompt": "Continue",
                    },
                )
        request.assert_not_called()

    def test_continue_transfers_parent_only_when_explicit(self) -> None:
        registration = ({"binding_id": "new-binding"}, True, {"binding_id": "old-binding"})
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            bridge, "_request", return_value=None
        ) as request, mock.patch.object(
            bridge, "register_binding", return_value=registration
        ) as register:
            result = bridge.call_tool(
                "opencode_continue",
                {
                    "workspace": folder,
                    "session_id": "ses_test",
                    "prompt": "Continue",
                    "codex_thread_id": "019f7fc8-2037-7b70-8dcd-b4439ed953e9",
                    "transfer_parent": True,
                },
            )
        self.assertTrue(result["structuredContent"]["persistent_parent_binding"])
        self.assertNotIn("model", request.call_args.kwargs["body"])
        register.assert_called_once_with(
            "ses_test",
            str(Path(folder).resolve()),
            "019f7fc8-2037-7b70-8dcd-b4439ed953e9",
            transfer_parent=True,
        )

    def test_continue_prompt_failure_restores_changed_binding(self) -> None:
        binding = {"binding_id": "new-binding"}
        previous = {"binding_id": "old-binding"}
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            bridge, "_request", side_effect=RuntimeError("prompt failed")
        ), mock.patch.object(
            bridge, "register_binding", return_value=(binding, True, previous)
        ), mock.patch.object(bridge, "rollback_binding") as rollback:
            with self.assertRaisesRegex(RuntimeError, "prompt failed"):
                bridge.call_tool(
                    "opencode_continue",
                    {
                        "workspace": folder,
                        "session_id": "ses_test",
                        "prompt": "Continue",
                        "codex_thread_id": "019f9a58-1f22-7f63-bd1c-7c480dd3ea1d",
                    },
                )
        rollback.assert_called_once_with("ses_test", "new-binding", previous)

    def test_detach_requires_exact_parent(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            bridge, "detach_binding", return_value=True
        ) as detach:
            result = bridge.call_tool(
                "opencode_detach",
                {
                    "workspace": folder,
                    "session_id": "ses_test",
                    "codex_thread_id": "019f9a58-1f22-7f63-bd1c-7c480dd3ea1d",
                },
            )
        self.assertTrue(result["structuredContent"]["detached"])
        detach.assert_called_once_with(
            "ses_test", "019f9a58-1f22-7f63-bd1c-7c480dd3ea1d"
        )

    def test_list_sessions_filters_sorts_and_limits(self) -> None:
        sessions = [
            {
                "id": "ses_old",
                "title": "Other task",
                "time": {"updated": 1},
                "secret": "omit me",
            },
            {
                "id": "ses_new",
                "title": "Bridge diagnosis",
                "time": {"updated": 3},
            },
            {
                "id": "ses_mid",
                "title": "Bridge follow-up",
                "time": {"updated": 2},
            },
        ]
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            bridge, "_request", return_value=sessions
        ):
            result = bridge.call_tool(
                "opencode_list_sessions",
                {"workspace": folder, "query": "bridge", "limit": 1},
            )
        payload = result["structuredContent"]
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["sessions"][0]["id"], "ses_new")
        self.assertNotIn("secret", payload["sessions"][0])

    def test_read_session_returns_text_without_reasoning_or_tools(self) -> None:
        session = {
            "id": "ses_test",
            "title": "Read me",
            "time": {"updated": 3},
        }
        messages = [
            {
                "info": {"id": "msg_user", "role": "user", "time": {"created": 1}},
                "parts": [{"type": "text", "text": "question"}],
            },
            {
                "info": {"id": "msg_assistant", "role": "assistant", "time": {"created": 2}},
                "parts": [
                    {"type": "reasoning", "text": "private chain"},
                    {"type": "tool", "state": {"input": "private tool input"}},
                    {"type": "text", "text": "answer"},
                ],
            },
            {
                "info": {"id": "msg_tool_only", "role": "assistant", "time": {"created": 3}},
                "parts": [{"type": "tool", "state": {"input": "private tool input"}}],
            },
        ]

        def fake_request(method, path, **kwargs):
            if path == "/session":
                return [session]
            if path.startswith("/session/ses_test/message?"):
                return messages
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            bridge, "_request", side_effect=fake_request
        ):
            result = bridge.call_tool(
                "opencode_read_session",
                {"workspace": folder, "session_id": "ses_test"},
            )
        payload = result["structuredContent"]
        self.assertEqual(payload["messages"][0]["text"], "question")
        self.assertEqual(payload["messages"][1]["text"], "answer")
        self.assertEqual(len(payload["messages"]), 2)
        self.assertEqual(payload["latest_assistant_text"], "answer")
        serialized = json.dumps(payload)
        self.assertNotIn("private chain", serialized)
        self.assertNotIn("private tool input", serialized)

    def test_read_session_selects_the_exact_terminal_receipt(self) -> None:
        session = {"id": "ses_test", "title": "Test", "directory": "C:\\Work"}
        assistant_id = "msg_exact"
        completion_id = bridge._terminal_completion_id(
            "ses_test",
            assistant_id,
            "completed",
        )
        messages = [
            {
                "info": {
                    "id": assistant_id,
                    "role": "assistant",
                    "time": {"created": 1, "completed": 2},
                },
                "parts": [{"type": "text", "text": "exact"}],
            },
            {
                "info": {
                    "id": "msg_later",
                    "role": "assistant",
                    "time": {"created": 3, "completed": 4},
                },
                "parts": [{"type": "text", "text": "later"}],
            },
        ]

        def fake_request(method, path, **kwargs):
            if path == "/session":
                return [session]
            if path.startswith("/session/ses_test/message?"):
                return messages
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            bridge, "_request", side_effect=fake_request
        ):
            result = bridge.call_tool(
                "opencode_read_session",
                {
                    "workspace": folder,
                    "session_id": "ses_test",
                    "completion_id": completion_id,
                    "phase": "completed",
                },
            )
        payload = result["structuredContent"]
        self.assertEqual(payload["latest_assistant_text"], "exact")
        self.assertEqual([message["id"] for message in payload["messages"]], [assistant_id])
        self.assertEqual(payload["completion_id"], completion_id)

    def test_permission_never_accepts_always(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                bridge.call_tool(
                    "opencode_reply_permission",
                    {
                        "workspace": folder,
                        "request_id": "per_test",
                        "reply": "always",
                    },
                )

    def test_extracts_latest_assistant_text(self) -> None:
        messages = [
            {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "old"}]},
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "question"}]},
            {
                "info": {"role": "assistant"},
                "parts": [
                    {"type": "reasoning", "text": "hidden"},
                    {"type": "text", "text": "answer"},
                ],
            },
        ]
        self.assertEqual(bridge._text_parts(messages), "answer")

    def test_petcrew_session_identity_is_sanitized_sha256(self) -> None:
        value = bridge._petcrew_session_id("  ses_test  ")
        self.assertRegex(value, r"^session:[0-9a-f]{64}$")
        self.assertEqual(value, bridge._petcrew_session_id("ses_test"))
        self.assertNotIn("ses_test", value)

    def test_matching_inbox_completion_uses_opaque_session_id(self) -> None:
        expected = bridge._petcrew_session_id("ses_test")
        payload = {
            "latest_cursor": 9,
            "truncated": False,
            "completions": [
                {
                    "cursor": 8,
                    "completion_id": "completion:" + "a" * 64,
                    "provider": "opencode",
                    "session_id": "session:" + "b" * 64,
                    "agent_id": "root:" + "b" * 64,
                    "parent_agent_id": None,
                    "phase": "completed",
                    "completed_at": "2026-07-26T12:00:00+03:00",
                },
                {
                    "cursor": 9,
                    "completion_id": "completion:" + "c" * 64,
                    "provider": "opencode",
                    "session_id": expected,
                    "agent_id": expected.replace("session:", "root:"),
                    "parent_agent_id": None,
                    "phase": "failed",
                    "completed_at": "2026-07-26T12:01:00+03:00",
                },
            ],
        }
        with mock.patch.object(bridge, "_petcrew_inbox", return_value=payload):
            completion, cursor = bridge._matching_inbox_completion("ses_test")
        self.assertEqual(cursor, 9)
        self.assertEqual(completion["phase"], "failed")
        self.assertEqual(completion["session_id"], expected)

    def test_wait_uses_petcrew_event_without_status_polling(self) -> None:
        assistant_id = "msg_done"
        completion = {
            "cursor": 4,
            "completion_id": bridge._terminal_completion_id(
                "ses_test",
                assistant_id,
                "completed",
            ),
            "provider": "opencode",
            "session_id": bridge._petcrew_session_id("ses_test"),
            "agent_id": "root:" + "e" * 64,
            "parent_agent_id": None,
            "phase": "completed",
            "completed_at": "2026-07-26T12:00:00+03:00",
        }
        requests = []

        def fake_request(method, path, **kwargs):
            requests.append((method, path))
            if path == "/session/status":
                return {"ses_test": {"type": "idle"}}
            if path.startswith("/session/ses_test/message?"):
                return [
                    {
                        "info": {
                            "id": assistant_id,
                            "role": "assistant",
                            "time": {"created": 1, "completed": 2},
                        },
                        "parts": [{"type": "text", "text": "done"}],
                    }
                ]
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            bridge, "_pending", return_value=([], [])
        ) as pending, mock.patch.object(
            bridge, "_matching_inbox_completion", return_value=(None, 3)
        ), mock.patch.object(
            bridge, "_petcrew_sse_completion", return_value=completion
        ) as sse, mock.patch.object(
            bridge, "_request", side_effect=fake_request
        ):
            result = bridge.call_tool(
                "opencode_wait",
                {"workspace": folder, "session_id": "ses_test", "timeout_seconds": 5},
            )
        payload = result["structuredContent"]
        self.assertEqual(payload["completion_source"], "petcrew_sse")
        self.assertEqual(payload["completion"]["phase"], "completed")
        self.assertEqual(payload["latest_text"], "done")
        self.assertEqual(requests.count(("GET", "/session/status")), 1)
        self.assertEqual(pending.call_count, 2)
        sse.assert_called_once_with("ses_test", 3, 5)

    def test_rpc_tool_errors_are_structured(self) -> None:
        response = bridge.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "opencode_continue", "arguments": {}},
            }
        )
        self.assertTrue(response["result"]["isError"])
        json.dumps(response, ensure_ascii=False)

    def test_server_failure_includes_bounded_redacted_stderr(self) -> None:
        old_password = bridge.SERVER_PASSWORD
        try:
            bridge.SERVER_PASSWORD = "secret-value"
            bridge.SERVER_STDERR_LINES.clear()
            bridge.SERVER_STDERR_LINES.extend(
                [
                    "first line",
                    "token=secret-value",
                    "EEXIST: file already exists",
                ]
            )
            message = str(bridge._server_failure("OpenCode server exited with code 1"))
        finally:
            bridge.SERVER_PASSWORD = old_password
            bridge.SERVER_STDERR_LINES.clear()
        self.assertIn("Captured stderr:", message)
        self.assertIn("EEXIST: file already exists", message)
        self.assertIn("[REDACTED]", message)
        self.assertNotIn("secret-value", message)

    def test_external_server_mode_never_spawns_child_process(self) -> None:
        old_external = bridge.EXTERNAL_SERVER_MODE
        old_password = bridge.SERVER_PASSWORD
        try:
            bridge.EXTERNAL_SERVER_MODE = True
            bridge.SERVER_PASSWORD = None
            with mock.patch.object(
                bridge, "_read_shared_password", return_value="shared-secret"
            ), mock.patch.object(
                bridge, "_health", return_value={"healthy": True, "version": "1.18.5"}
            ), mock.patch.object(bridge.subprocess, "Popen") as popen:
                result = bridge.ensure_server()
            self.assertTrue(result["healthy"])
            popen.assert_not_called()
        finally:
            bridge.EXTERNAL_SERVER_MODE = old_external
            bridge.SERVER_PASSWORD = old_password

    def test_external_health_retries_one_cold_timeout_without_spawning(self) -> None:
        old_external = bridge.EXTERNAL_SERVER_MODE
        old_password = bridge.SERVER_PASSWORD
        try:
            bridge.EXTERNAL_SERVER_MODE = True
            bridge.SERVER_PASSWORD = None
            with mock.patch.object(
                bridge, "_read_shared_password", return_value="shared-secret"
            ), mock.patch.object(
                bridge,
                "_health",
                side_effect=[None, {"healthy": True, "version": "1.18.5"}],
            ) as health, mock.patch.object(
                bridge.time, "sleep"
            ) as sleep, mock.patch.object(bridge.subprocess, "Popen") as popen:
                result = bridge.ensure_server()
            self.assertTrue(result["healthy"])
            self.assertEqual(health.call_count, 2)
            self.assertEqual(
                [call.kwargs["timeout"] for call in health.call_args_list],
                [1.5, 3.0],
            )
            sleep.assert_called_once_with(0.2)
            popen.assert_not_called()
        finally:
            bridge.EXTERNAL_SERVER_MODE = old_external
            bridge.SERVER_PASSWORD = old_password

    def test_external_health_uses_bounded_readiness_window_and_fails_closed(self) -> None:
        old_external = bridge.EXTERNAL_SERVER_MODE
        old_password = bridge.SERVER_PASSWORD
        try:
            bridge.EXTERNAL_SERVER_MODE = True
            bridge.SERVER_PASSWORD = None
            with mock.patch.object(
                bridge, "_read_shared_password", return_value="shared-secret"
            ), mock.patch.object(
                bridge, "_health", return_value=None
            ) as health, mock.patch.object(
                bridge.time, "sleep"
            ) as sleep, mock.patch.object(bridge.subprocess, "Popen") as popen:
                with self.assertRaisesRegex(
                    RuntimeError, "External OpenCode server is not reachable"
                ):
                    bridge.ensure_server()
            self.assertEqual(
                [call.kwargs["timeout"] for call in health.call_args_list],
                [1.5, 3.0, 5.0],
            )
            self.assertEqual(
                [call.args[0] for call in sleep.call_args_list],
                [0.2, 0.4],
            )
            popen.assert_not_called()
        finally:
            bridge.EXTERNAL_SERVER_MODE = old_external
            bridge.SERVER_PASSWORD = old_password


if __name__ == "__main__":
    unittest.main()
