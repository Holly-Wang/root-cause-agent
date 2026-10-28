import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

from rca_agent import CancellationToken, RunControl, RunStopped, run_analysis
from rca_agent.model import ModelError
from rca_agent.report import RunResult
from tests.test_discovery import conclusion, tool_call
from tests.test_resource_limits import wire_size


ModelStep = dict[str, Any] | ModelError | Callable[[dict[str, Any], RunControl], dict[str, Any]]


class RecoveryModel:
    """仅替代外部模型通信，记录每次实际到达通信 interface 的完整输入。"""

    def __init__(self, *steps: ModelStep) -> None:
        self.steps = list(steps)
        self.requests: list[dict[str, Any]] = []
        self.deadlines: list[float] = []

    def complete(self, request: dict[str, Any], control: RunControl) -> dict[str, Any]:
        control.checkpoint()
        self.requests.append(json.loads(json.dumps(request)))
        self.deadlines.append(control.deadline)
        step = self.steps.pop(0)
        if isinstance(step, ModelError):
            raise step
        return step(request, control) if callable(step) else step


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "material"
        self.root.mkdir()
        self.source = "def average(values):\n    return sum(values) / 2\n"
        (self.root / "source.py").write_text(self.source, encoding="utf-8")
        self.config: dict[str, Any] = {
            "question": "调查三项输入的平均值错误。", "initial_materials": ["用户报告，尚未验证。"],
            "authorized_dir": str(self.root), "model": "deepseek-flash",
            "limits": {"max_model_calls": 8, "max_tool_calls": 8, "total_seconds": 5},
        }

    def recovery_chain(self, final: dict[str, Any] | None = None) -> RecoveryModel:
        return RecoveryModel(
            ModelError("request_timeout", retryable=True),
            tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2}, "read"),
            conclusion("E999"), ModelError("request_timeout", retryable=True),
            final if final is not None else conclusion("E2"),
            conclusion("E2"),  # 额外调用会消耗它，不能靠替身耗尽来假装正确停止。
        )

    def assert_saved(self, result: RunResult, name: str = "output") -> dict[str, Any]:
        output = Path(self.temp.name) / name
        result.write_to(output)
        saved = json.loads((output / "run.json").read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            self.fail("保存产物应为 JSON 对象。")
        self.assertEqual((saved["status"], saved["reason"]), (result.status, result.reason))
        self.assertEqual((saved["model_calls"], saved["tool_calls"]), (result.model_calls, result.tool_calls))
        self.assertEqual([item["id"] for item in saved["evidence"]], ["E1", "E2"])
        self.assertEqual(saved["evidence"][1]["content"], self.source)
        self.assertEqual(saved["evidence"][1]["source"], "source.py")
        self.assertEqual((saved["evidence"][1]["start_line"], saved["evidence"][1]["end_line"]), (1, 2))
        markdown = (output / "report.md").read_text(encoding="utf-8")
        self.assertIn(self.source.strip(), markdown.replace("> ", ""))
        self.assertIn(result.reason, markdown)
        self.assertEqual(saved["events"][-1], {"kind": "finished", "detail": f"{result.status}: {result.reason}"})
        return saved

    def test_communication_retries_and_report_repair_can_complete_together(self) -> None:
        model = self.recovery_chain()
        result = run_analysis(self.config, transport=model)
        self.assertEqual((result.status, result.reason), ("completed", "valid_report"))
        self.assertEqual((result.model_calls, result.tool_calls, len(model.requests)), (5, 1, 5))
        self.assertEqual(len(model.steps), 1)
        self.assertEqual(model.requests[0], model.requests[1])
        self.assertEqual(model.requests[3], model.requests[4])
        self.assertEqual(len(set(model.deadlines)), 1)
        repair_request = model.requests[3]
        feedback = json.loads(repair_request["messages"][-1]["content"])
        self.assertEqual((feedback["type"], feedback["error"]), ("report_validation_feedback", "unknown_evidence"))
        available_file = next(e for e in feedback["available_evidence"] if e["evidence_id"] == "E2")
        self.assertEqual((available_file["kind"], available_file["start_line"], available_file["end_line"]), ("file", 1, 2))
        tools = [message for message in repair_request["messages"] if message["role"] == "tool"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["tool_call_id"], "read")
        self.assertEqual(json.loads(tools[0]["content"])["evidence"]["content"], self.source)
        self.assertEqual([e.detail for e in result.events if e.kind == "model_request_failed"],
                         ["request_timeout", "request_timeout"])
        self.assertEqual(sum(e.kind == "report_validation_feedback" for e in result.events), 1)
        self.assertEqual(sum(e.kind == "tool_attempt" for e in result.events), 1)
        self.assertEqual(sum(e.kind == "evidence_saved" and e.detail == "E2" for e in result.events), 1)
        saved = self.assert_saved(result)
        self.assertEqual(saved["report"]["candidate_causes"][0]["supporting_evidence"],
                         [{"evidence_id": "E2", "start_line": 2, "end_line": 2}])
        self.assertEqual(saved["config_snapshot"]["limits"]["max_model_calls"], 8)

    def test_permanent_error_after_evidence_is_not_retried(self) -> None:
        model = RecoveryModel(
            tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2}),
            ModelError("missing_api_key"), conclusion("E2"),
        )
        result = run_analysis(self.config, transport=model)
        self.assertEqual((result.status, result.reason), ("system_failure", "missing_api_key"))
        self.assertEqual((result.model_calls, result.tool_calls, len(model.requests)), (2, 1, 2))
        self.assertEqual(len(model.steps), 1)
        self.assertFalse(any(e.kind == "report_validation_feedback" for e in result.events))
        saved = self.assert_saved(result)
        self.assertIsNone(saved["report"])
        self.assertEqual([e["detail"] for e in saved["events"] if e["kind"] == "model_request_failed"], ["missing_api_key"])

    def test_two_transient_failures_of_the_same_request_stop_with_evidence(self) -> None:
        model = RecoveryModel(
            tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2}),
            ModelError("request_timeout", retryable=True), ModelError("request_timeout", retryable=True),
            conclusion("E2"),
        )
        result = run_analysis(self.config, transport=model)
        self.assertEqual((result.status, result.reason), ("system_failure", "request_timeout"))
        self.assertEqual((result.model_calls, result.tool_calls, len(model.requests)), (3, 1, 3))
        self.assertEqual(model.requests[1], model.requests[2])
        self.assertEqual(len(model.steps), 1)
        saved = self.assert_saved(result)
        self.assertIsNone(saved["report"])
        self.assertEqual([e["detail"] for e in saved["events"] if e["kind"] == "model_request_failed"],
                         ["request_timeout", "request_timeout"])

    def test_retried_repair_with_an_invalid_range_cannot_get_another_repair(self) -> None:
        invalid = conclusion("E2")
        report = json.loads(invalid["choices"][0]["message"]["content"])
        report["candidate_causes"][0]["supporting_evidence"] = [
            {"evidence_id": "E2", "start_line": 3, "end_line": 3},
        ]
        invalid["choices"][0]["message"]["content"] = json.dumps(report)
        model = self.recovery_chain(invalid)
        result = run_analysis(self.config, transport=model)
        self.assertEqual((result.status, result.reason), ("system_failure", "invalid_report: invalid_reference_range"))
        self.assertEqual((result.model_calls, result.tool_calls, len(model.requests)), (5, 1, 5))
        self.assertEqual(len(model.steps), 1)
        self.assertEqual(model.requests[3], model.requests[4])
        saved = self.assert_saved(result)
        self.assertIsNone(saved["report"])
        feedback = [json.loads(e["detail"]) for e in saved["events"] if e["kind"] == "report_validation_feedback"]
        self.assertEqual([item["error"] for item in feedback], ["unknown_evidence"])
        self.assertEqual(sum(e["kind"] == "model_request_failed" for e in saved["events"]), 2)

    def test_four_model_attempts_block_the_fifth_recovery_attempt(self) -> None:
        self.config["limits"]["max_model_calls"] = 4
        model = self.recovery_chain()
        result = run_analysis(self.config, transport=model)
        self.assertEqual((result.status, result.reason), ("limits_exhausted", "call_limit"))
        self.assertEqual((result.model_calls, result.tool_calls, len(model.requests)), (4, 1, 4))
        self.assertEqual(len(model.steps), 2)
        self.assertEqual(model.requests[0], model.requests[1])
        saved = self.assert_saved(result)
        self.assertIsNone(saved["report"])
        self.assertEqual(sum(e["kind"] == "model_attempt" for e in saved["events"]), 4)
        self.assertEqual([e["detail"] for e in saved["events"] if e["kind"] == "model_request_failed"],
                         ["request_timeout", "request_timeout"])
        feedback = [json.loads(e["detail"]) for e in saved["events"] if e["kind"] == "report_validation_feedback"]
        self.assertEqual([item["error"] for item in feedback], ["unknown_evidence"])

    def test_repair_after_a_communication_retry_still_requires_context_admission(self) -> None:
        probe = self.recovery_chain()
        self.assertEqual(run_analysis(self.config, transport=probe).status, "completed")
        self.config["limits"]["max_context_bytes"] = wire_size(probe.requests[2])
        model = self.recovery_chain()
        result = run_analysis(self.config, transport=model)
        self.assertEqual((result.status, result.reason), ("limits_exhausted", "max_context_bytes"))
        self.assertEqual((result.model_calls, result.tool_calls, len(model.requests)), (3, 1, 3))
        self.assertEqual(len(model.steps), 3)
        self.assertEqual(model.requests[0], model.requests[1])
        saved = self.assert_saved(result)
        self.assertIsNone(saved["report"])
        self.assertEqual(sum(e["kind"] == "model_attempt" for e in saved["events"]), 3)
        self.assertEqual(sum(e["kind"] == "model_request_failed" for e in saved["events"]), 1)
        feedback = [json.loads(e["detail"]) for e in saved["events"] if e["kind"] == "report_validation_feedback"]
        self.assertEqual([item["error"] for item in feedback], ["unknown_evidence"])
        refusal = next(json.loads(e["detail"]) for e in saved["events"] if e["kind"] == "resource_limit")
        self.assertEqual(refusal["resource"], "max_context_bytes")
        self.assertGreater(refusal["actual_bytes"], refusal["limit_bytes"])

    def test_stop_during_repair_prevents_recovery_or_completion_from_late_results(self) -> None:
        for reason in ("user_cancelled", "total_deadline"):
            for outcome in ("valid_report", "retryable_error"):
                with self.subTest(reason=reason, outcome=outcome):
                    cancellation = CancellationToken()
                    self.config["limits"]["total_seconds"] = 5 if reason == "user_cancelled" else 0.3
                    closed: list[bool] = []
                    closed_at_finish: list[bool] = []

                    def late(request: dict[str, Any], control: RunControl) -> dict[str, Any]:
                        try:
                            if reason == "user_cancelled":
                                cancellation.cancel()
                            else:
                                try:
                                    control.wait()
                                except RunStopped:
                                    pass  # 验证已收尾的晚到通信结果仍不能越过宿主停止检查。
                            if outcome == "retryable_error":
                                raise ModelError("request_timeout", retryable=True)
                            return conclusion("E2")
                        finally:
                            closed.append(True)

                    model = RecoveryModel(
                        ModelError("request_timeout", retryable=True),
                        tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2}),
                        conclusion("E999"), late, conclusion("E2"),
                    )
                    result = run_analysis(
                        self.config, transport=model, cancellation=cancellation,
                        on_event=lambda event: closed_at_finish.append(bool(closed)) if event.kind == "finished" else None,
                    )
                    self.assertEqual((result.status, result.reason),
                                     ("user_cancelled" if reason == "user_cancelled" else "timeout", reason))
                    self.assertEqual((result.model_calls, result.tool_calls, len(model.requests)), (4, 1, 4))
                    self.assertEqual(len(model.steps), 1)
                    self.assertEqual(len(set(model.deadlines)), 1)
                    self.assertEqual((closed, closed_at_finish), ([True], [True]))
                    saved = self.assert_saved(result, reason + "-" + outcome)
                    self.assertIsNone(saved["report"])
                    self.assertEqual([e["detail"] for e in saved["events"] if e["kind"] == "model_request_failed"],
                                     ["request_timeout"])
                    feedback = [json.loads(e["detail"]) for e in saved["events"] if e["kind"] == "report_validation_feedback"]
                    self.assertEqual([item["error"] for item in feedback], ["unknown_evidence"])

    def test_malformed_json_repair_can_recover_from_a_transient_communication_error(self) -> None:
        malformed = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "{"}}]}
        model = RecoveryModel(
            tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2}),
            malformed, ModelError("request_timeout", retryable=True), conclusion("E2"), conclusion("E2"),
        )
        result = run_analysis(self.config, transport=model)
        self.assertEqual((result.status, result.reason), ("completed", "valid_report"))
        self.assertEqual((result.model_calls, result.tool_calls, len(model.requests)), (4, 1, 4))
        self.assertEqual(model.requests[2], model.requests[3])
        self.assertEqual(len(model.steps), 1)
        saved = self.assert_saved(result)
        feedback = [json.loads(e["detail"]) for e in saved["events"] if e["kind"] == "report_validation_feedback"]
        self.assertEqual([item["error"] for item in feedback], ["invalid_json"])
        self.assertEqual([e["detail"] for e in saved["events"] if e["kind"] == "model_request_failed"], ["request_timeout"])
