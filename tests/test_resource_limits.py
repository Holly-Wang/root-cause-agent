import json
import os
import sys
import threading
import time
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from typing import Any

from rca_agent import run_analysis
from rca_agent.config import ConfigError
from rca_agent.control import CancellationToken
from rca_agent.model import ModelError
from tests.test_discovery import ScriptedModel, conclusion, tool_call


def wire_size(value: Any) -> int:
    # 独立按公开计量约定核对公共通信边界，不调用生产代码的计量函数。
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":")).encode("utf-8"))


class ResourceLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "material"
        self.root.mkdir()
        (self.root / "source.py").write_text("def average(values):\n    return sum(values) / 2\n", encoding="utf-8")
        self.config: dict[str, Any] = {
            "question": "平均值为什么错误？",
            "initial_materials": ["用户材料"],
            "authorized_dir": str(self.root), "model": "deepseek-flash",
            "limits": {"max_model_calls": 12, "max_tool_calls": 12, "total_seconds": 5},
        }

    def test_initial_context_is_rejected_before_any_model_attempt(self) -> None:
        self.config["limits"]["max_context_bytes"] = 1
        model = ScriptedModel([])
        result = run_analysis(self.config, transport=model)
        self.assertEqual((result.status, result.reason), ("limits_exhausted", "max_context_bytes"))
        self.assertEqual((result.model_calls, result.tool_calls, model.requests), (0, 0, []))
        self.assertEqual(result.evidence[0].content, "用户材料")
        self.assertIn("max_context_bytes", result.markdown)
        self.assertFalse(any(event.kind == "model_attempt" for event in result.events))

    def test_oversized_assistant_history_stops_before_dispatching_tools(self) -> None:
        read = tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2})
        probe = ScriptedModel([read, conclusion("E2")])
        self.assertEqual(run_analysis(self.config, transport=probe).status, "completed")
        self.config["limits"]["max_context_bytes"] = wire_size(probe.requests[0]) + 32
        oversized = tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2})
        oversized["choices"][0]["message"]["content"] = "调查说明" * 2000
        model = ScriptedModel([oversized])
        result = run_analysis(self.config, transport=model)
        self.assertEqual((result.status, result.reason), ("limits_exhausted", "max_context_bytes"))
        self.assertEqual((result.model_calls, result.tool_calls, len(model.requests)), (1, 0, 1))
        self.assertEqual([e.kind for e in result.evidence], ["initial_material"])

    def test_initial_evidence_is_admitted_sequentially_and_rejection_is_saved_without_content(self) -> None:
        first = {"id": "E1", "kind": "initial_material", "source": "initial_materials[0]",
                 "content": "用户材料", "start_line": None, "end_line": None}
        budget = wire_size(first)
        self.config["initial_materials"].append("不应存储的拒绝原文" * 100)
        self.config["limits"]["max_evidence_bytes"] = budget
        model = ScriptedModel([])
        result = run_analysis(self.config, transport=model)
        self.assertEqual((result.status, result.reason), ("limits_exhausted", "max_evidence_bytes"))
        self.assertEqual([e.content for e in result.evidence], ["用户材料"])
        self.assertEqual((result.model_calls, result.tool_calls, model.requests), (0, 0, []))
        output = Path(self.temp.name) / "report"
        result.write_to(output)
        saved_text = (output / "run.json").read_text(encoding="utf-8")
        saved = json.loads(saved_text)
        self.assertEqual(saved["resource_usage"]["evidence_bytes"], budget)
        rejection = next(json.loads(e["detail"]) for e in saved["events"] if e["kind"] == "resource_limit")
        self.assertEqual(rejection["source"], "initial_materials[1]")
        self.assertGreater(rejection["actual_bytes"], budget)
        self.assertNotIn("不应存储的拒绝原文", saved_text)
        markdown = (output / "report.md").read_text(encoding="utf-8")
        self.assertIn("用户材料", markdown)
        self.assertIn("initial_materials[1]", markdown)
        self.assertNotIn("不应存储的拒绝原文", markdown)

    def test_each_tool_kind_and_repeated_snapshot_counts_toward_evidence_limit(self) -> None:
        self.config["permitted_tests"] = {"check": {
            "command": [sys.executable, "-c", "print('可信测试输出')"], "timeout_seconds": 2,
        }}
        choices = [
            tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2}),
            tool_call("list_directory", {"path": "."}),
            tool_call("search_text", {"path": "source.py", "query": "sum"}),
            tool_call("run_test", {"test_id": "check"}),
        ]
        for choice in choices:
            with self.subTest(tool=choice["choices"][0]["message"]["tool_calls"][0]["function"]["name"]):
                self.config["limits"].pop("max_evidence_bytes", None)
                probe = ScriptedModel([
                    choice, tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2}),
                    conclusion("E3"),
                ])
                observed = run_analysis(self.config, transport=probe)
                self.assertEqual(observed.status, "completed", observed.reason)
                first_bytes = sum(wire_size(asdict(e)) for e in observed.evidence[:2])
                # 完整首份快照可准入；同一合法操作再执行也必须另算，不按内容去重。
                self.config["limits"]["max_evidence_bytes"] = first_bytes + 64
                model = ScriptedModel([choice, choice, conclusion("E2")])
                result = run_analysis(self.config, transport=model)
                self.assertEqual((result.status, result.reason), ("limits_exhausted", "max_evidence_bytes"))
                self.assertEqual((result.model_calls, result.tool_calls, len(model.requests)), (2, 2, 2))
                self.assertEqual(len(result.evidence), 2)
                self.assertEqual(result.evidence[1].kind, observed.evidence[1].kind)
                self.assertLessEqual(result.resource_usage["evidence_bytes"], self.config["limits"]["max_evidence_bytes"])
                rejection = next(json.loads(e.detail) for e in result.events if e.kind == "resource_limit")
                self.assertEqual(rejection["evidence_id"], "E3")
                self.assertGreater(rejection["actual_bytes"], rejection["limit_bytes"])
                if result.evidence[1].kind == "test":
                    self.assertEqual(result.cleanup_status, "confirmed")
                    self.assertEqual(len(result.cleanup), 2)

    def test_complete_request_utf8_boundary_includes_tools_and_all_fields(self) -> None:
        read = tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2})
        self.config["question"] = '中文、引号"与换行\n'
        probe = ScriptedModel([read, conclusion("E2")])
        self.assertEqual(run_analysis(self.config, transport=probe).status, "completed")
        budget = max(wire_size(request) for request in probe.requests)
        self.config["limits"]["max_context_bytes"] = budget
        equal_model = ScriptedModel([read, conclusion("E2")])
        equal = run_analysis(self.config, transport=equal_model)
        self.assertEqual(equal.status, "completed", equal.reason)
        self.assertEqual(equal.resource_usage["largest_context_bytes"], budget)
        self.config["limits"]["max_context_bytes"] = budget - 1
        smaller_model = ScriptedModel([read, conclusion("E2")])
        smaller = run_analysis(self.config, transport=smaller_model)
        self.assertEqual((smaller.status, smaller.reason), ("limits_exhausted", "max_context_bytes"))
        self.assertEqual((smaller.model_calls, smaller.tool_calls, len(smaller_model.requests)), (1, 1, 1))
        self.assertEqual(len(smaller.evidence), 2)
        initial_request = probe.requests[0]
        # 仅 messages 的尺寸可以容纳，工具 schema 和其他字段使完整请求超限。
        self.config["limits"]["max_context_bytes"] = wire_size(initial_request["messages"])
        schema_model = ScriptedModel([])
        schema = run_analysis(self.config, transport=schema_model)
        self.assertEqual((schema.reason, schema.model_calls, schema_model.requests), ("max_context_bytes", 0, []))

    def test_tool_feedback_growth_stops_the_remaining_calls_in_the_same_batch(self) -> None:
        read = tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2})
        probe = ScriptedModel([read, conclusion("E2")])
        run_analysis(self.config, transport=probe)
        batch = tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2})
        batch["choices"][0]["message"]["tool_calls"].extend(
            tool_call("run_test", {"test_id": "not-permitted"}, "second")["choices"][0]["message"]["tool_calls"]
        )
        before_feedback = json.loads(json.dumps(probe.requests[0]))
        before_feedback["messages"].append(batch["choices"][0]["message"])
        self.config["limits"]["max_context_bytes"] = wire_size(before_feedback)
        model = ScriptedModel([batch])
        result = run_analysis(self.config, transport=model)
        self.assertEqual((result.status, result.reason), ("limits_exhausted", "max_context_bytes"))
        self.assertEqual((result.model_calls, result.tool_calls, len(model.requests)), (1, 1, 1))
        self.assertEqual([e.kind for e in result.evidence], ["initial_material", "file"])
        self.assertLess(result.resource_usage["evidence_bytes"], 1000)
        self.assertFalse(any(e.kind == "tool_rejected" for e in result.events))

    def test_report_repair_feedback_is_size_checked_without_an_extra_model_attempt(self) -> None:
        read = tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2})
        invalid = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "{}"}}]}
        probe = ScriptedModel([read, invalid, conclusion("E2")])
        self.assertEqual(run_analysis(self.config, transport=probe).status, "completed")
        self.config["limits"]["max_context_bytes"] = wire_size(probe.requests[1])
        model = ScriptedModel([read, invalid, conclusion("E2")])
        result = run_analysis(self.config, transport=model)
        self.assertEqual((result.status, result.reason), ("limits_exhausted", "max_context_bytes"))
        self.assertEqual((result.model_calls, result.tool_calls, len(model.requests)), (2, 1, 2))
        self.assertIsNone(result.report)
        self.assertEqual(len(result.evidence), 2)
        self.assertEqual(sum(e.kind == "report_validation_feedback" for e in result.events), 1)
        self.assertIn("return sum(values) / 2", result.markdown)

    def test_retry_reuses_context_and_counts_then_growth_is_rejected(self) -> None:
        read = tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2})
        probe = ScriptedModel([read, conclusion("E2")])
        run_analysis(self.config, transport=probe)
        self.config["limits"]["max_context_bytes"] = wire_size(probe.requests[0])

        def transient(request: dict[str, Any]) -> dict[str, Any]:
            raise ModelError("request_timeout", retryable=True)

        model = ScriptedModel([transient, {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "{}",
        }}]}, conclusion("E2")])
        result = run_analysis(self.config, transport=model)
        self.assertEqual((result.status, result.reason), ("limits_exhausted", "max_context_bytes"))
        self.assertEqual((result.model_calls, result.tool_calls, len(model.requests)), (2, 0, 2))
        self.assertEqual(model.requests[0], model.requests[1])
        self.assertEqual(sum(e.kind == "model_request_failed" for e in result.events), 1)

    def test_rejected_tools_still_exhaust_the_original_call_budget(self) -> None:
        self.config["limits"].update({"max_context_bytes": 262144, "max_evidence_bytes": 1000,
                                      "max_tool_calls": 2})
        denied = tool_call("read_file", {"path": "../outside", "start_line": 1, "end_line": 2})
        model = ScriptedModel([denied, denied, conclusion("E2")])
        result = run_analysis(self.config, transport=model)
        self.assertEqual((result.status, result.reason), ("limits_exhausted", "call_limit"))
        self.assertEqual((result.model_calls, result.tool_calls, len(model.requests)), (2, 2, 2))
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(sum(e.kind == "tool_rejected" for e in result.events), 2)

    def test_new_limits_require_positive_integers_before_dispatch(self) -> None:
        for name in ("max_context_bytes", "max_evidence_bytes"):
            for value in (0, -1, True, 1.5, "100", None):
                with self.subTest(limit=name, value=value):
                    self.config["limits"][name] = value
                    model = ScriptedModel([])
                    with self.assertRaisesRegex(ConfigError, name):
                        run_analysis(self.config, transport=model)
                    self.assertEqual(model.requests, [])
            self.config["limits"].pop(name)

    def test_one_byte_evidence_budget_retains_no_records_and_stops_before_model(self) -> None:
        self.config["limits"]["max_evidence_bytes"] = 1
        model = ScriptedModel([])
        result = run_analysis(self.config, transport=model)
        self.assertEqual((result.status, result.reason), ("limits_exhausted", "max_evidence_bytes"))
        self.assertEqual((result.evidence, result.resource_usage["evidence_bytes"], model.requests), ([], 0, []))

    def test_primary_stop_survives_rejection_of_a_real_test_result(self) -> None:
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                ready = self.root / ("cancel-ready" if cancel else "deadline-ready")
                command = [sys.executable, "-u", "-c",
                           "import os,time\nfrom pathlib import Path\n"
                           f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
                           "print('READY', flush=True)\nwhile True: time.sleep(1)\n"]
                self.config["permitted_tests"] = {"wait": {"command": command, "timeout_seconds": 20}}
                self.config["limits"].update({"max_evidence_bytes": 200, "total_seconds": 5 if cancel else 1})
                cancellation = CancellationToken()

                def cancel_when_ready() -> None:
                    until = time.monotonic() + 3
                    while not ready.exists() and time.monotonic() < until:
                        threading.Event().wait(0.01)
                    if ready.exists():
                        cancellation.cancel()

                controller = threading.Thread(target=cancel_when_ready)
                if cancel:
                    controller.start()
                model = ScriptedModel([tool_call("run_test", {"test_id": "wait"})])
                try:
                    result = run_analysis(self.config, transport=model, cancellation=cancellation)
                finally:
                    if cancel:
                        controller.join(4)
                self.assertTrue(ready.exists(), "必须观察到真实测试已启动。")
                expected = ("user_cancelled", "user_cancelled") if cancel else ("timeout", "total_deadline")
                self.assertEqual((result.status, result.reason), expected)
                self.assertEqual((result.model_calls, result.tool_calls, len(model.requests)), (1, 1, 1))
                self.assertEqual([e.kind for e in result.evidence], ["initial_material"])
                self.assertTrue(any(e.kind == "resource_limit" and "max_evidence_bytes" in e.detail for e in result.events))
                self.assertEqual(result.cleanup_status, "confirmed")
                self.assertLessEqual(result.cleanup_seconds, 2.05)
                with self.assertRaises(ProcessLookupError):
                    os.killpg(result.cleanup[0].process_group_id, 0)

    def test_cancellation_before_admission_keeps_its_primary_reason(self) -> None:
        cancellation = CancellationToken()
        cancellation.cancel()
        self.config["limits"].update({"max_context_bytes": 1, "max_evidence_bytes": 1})
        model = ScriptedModel([])
        result = run_analysis(self.config, transport=model, cancellation=cancellation)
        self.assertEqual((result.status, result.reason), ("user_cancelled", "user_cancelled"))
        self.assertEqual((result.model_calls, result.tool_calls, model.requests), (0, 0, []))

    def test_large_initial_material_can_fit_evidence_but_not_the_full_request(self) -> None:
        self.config["initial_materials"] = ["原始材料" * 2000]
        self.config["limits"]["max_context_bytes"] = 8192
        model = ScriptedModel([])
        result = run_analysis(self.config, transport=model)
        self.assertEqual((result.status, result.reason), ("limits_exhausted", "max_context_bytes"))
        self.assertEqual((result.model_calls, result.tool_calls, model.requests), (0, 0, []))
        self.assertEqual(result.evidence[0].content, self.config["initial_materials"][0])
        self.assertLess(result.resource_usage["evidence_bytes"], 131072)
        self.assertGreater(result.resource_usage["largest_context_bytes"], 8192)
        self.assertEqual(result.config_snapshot["limits"]["max_evidence_bytes"], 131072)
