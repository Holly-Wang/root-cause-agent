import json
import os
import subprocess
import sys
import time
import tempfile
import unittest
from pathlib import Path
from typing import Any

from rca_agent import run_analysis
from rca_agent.control import RunControl


def read_call(path: str, start: int = 1, end: int = 2) -> dict[str, Any]:
    return {
        "choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "read-1", "type": "function", "function": {
                "name": "read_file",
                "arguments": json.dumps({"path": path, "start_line": start, "end_line": end}),
            }}],
        }}],
    }


def final_response(evidence_id: str = "E2") -> dict[str, Any]:
    report = {
        "root_cause_found": False, "information_gaps": ["尚缺获准测试的验证结果。"],
        "follow_up_questions": ["能否提供实际输入和测试结果？"],
        "candidate_causes": [{
            "summary": "除数使用固定值 2，三项输入的平均值因此错误。",
            "supporting_evidence": [{"evidence_id": evidence_id, "start_line": 2, "end_line": 2}],
            "contradicting_evidence": [],
        }],
        "verification": [],
        "unconfirmed": ["尚未运行测试验证此候选原因。"],
        "repair_suggestions": ["检查并改为按实际项目数求平均值。"],
        "next_steps": ["对三项输入执行获准测试。"],
    }
    return {"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": json.dumps(report, ensure_ascii=False),
    }}]}


class SequenceTransport:
    """只替代外部模型 API；请求解析、取证、报告仍使用实际程序。"""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def complete(self, request: dict[str, Any], control: RunControl) -> dict[str, Any]:
        control.checkpoint()
        self.requests.append(json.loads(json.dumps(request)))
        return self.responses.pop(0)


class AnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "material"
        self.root.mkdir()
        (self.root / "average.py").write_text(
            "def average(values):\n    return sum(values) / 2\n", encoding="utf-8"
        )
        self.config: dict[str, Any] = {
            "question": "average([3, 6, 9]) 得到 9，预期 6。为什么？",
            "initial_materials": ["用户提供的现象：预期 6，实际 9；尚未在本次运行验证。"],
            "authorized_dir": str(self.root),
            "model": "deepseek-flash",
            "limits": {"max_model_calls": 6, "max_tool_calls": 6,
                       "total_seconds": 5, "max_read_bytes": 8192},
        }

    def test_model_reads_real_file_and_report_cites_snapshot(self) -> None:
        transport = SequenceTransport([read_call("average.py"), final_response()])
        result = run_analysis(self.config, transport=transport)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.evidence[1].content, "def average(values):\n    return sum(values) / 2\n")
        self.assertEqual(result.evidence[1].source, "average.py")
        feedback = json.loads(transport.requests[1]["messages"][-1]["content"])
        self.assertEqual(feedback["evidence"]["id"], "E2")
        self.assertEqual(feedback["evidence"]["content"], result.evidence[1].content)
        self.assertIn("E2", result.markdown)
        self.assertIn("尚未运行测试", result.markdown)
        self.assertIn("未取得反驳证据", result.markdown)
        (self.root / "average.py").write_text("changed\n", encoding="utf-8")
        self.assertIn("return sum(values) / 2", result.evidence[1].content)

    def test_cli_saves_failure_without_exposing_environment(self) -> None:
        config_file = Path(self.temp.name) / "config.json"
        config_file.write_text(json.dumps(self.config), encoding="utf-8")
        output = Path(self.temp.name) / "output"
        env = os.environ.copy()
        env.pop("DEEPSEEK_API_KEY", None)
        process = subprocess.run(
            [sys.executable, "-m", "rca_agent", "--config", str(config_file), "--output", str(output)],
            env=env, text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(process.returncode, 1, process.stderr)
        saved = json.loads((output / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "system_failure")
        self.assertEqual(saved["reason"], "missing_api_key")
        self.assertTrue((output / "report.md").is_file())
        self.assertIn("finished", process.stderr)

    def test_saved_run_distinguishes_simulation_and_records_frozen_config(self) -> None:
        result = run_analysis(self.config, transport=SequenceTransport([read_call("average.py"), final_response()]))
        output = Path(self.temp.name) / "saved"
        self.config["limits"]["max_model_calls"] = 100
        result.write_to(output)
        saved = json.loads((output / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["model_source"], "injected_transport")
        self.assertEqual(saved["config_snapshot"]["limits"]["max_model_calls"], 6)
        self.assertEqual(saved["config_snapshot"]["authorized_dir"], str(self.root.resolve()))

    def test_outside_path_and_symlink_are_rejected_then_model_can_adjust(self) -> None:
        outside = Path(self.temp.name) / "private.txt"
        outside.write_text("outside material must not enter evidence", encoding="utf-8")
        (self.root / "external.txt").symlink_to(outside)
        for path in ("../private.txt", "external.txt"):
            with self.subTest(path=path):
                transport = SequenceTransport([read_call(path), read_call("average.py"), final_response()])
                result = run_analysis(self.config, transport=transport)
                rejected = json.loads(transport.requests[1]["messages"][-1]["content"])
                self.assertEqual(rejected["error"], "permission_denied")
                self.assertEqual(result.status, "completed")
                self.assertEqual([e.kind for e in result.evidence], ["initial_material", "file"])
                self.assertNotIn("outside material", result.markdown)

    def test_model_can_read_again_using_actual_feedback(self) -> None:
        transport = SequenceTransport([read_call("average.py", 1, 1), read_call("average.py", 2, 2),
                                       final_response("E3")])
        result = run_analysis(self.config, transport=transport)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.evidence[2].start_line, 2)
        self.assertEqual(result.evidence[2].content, "    return sum(values) / 2\n")
        self.assertIn("E3 行 2–2", result.markdown)

    def test_invalid_config_is_rejected_before_any_model_request(self) -> None:
        for invalid in (False, 0, -1, "8"):
            with self.subTest(invalid=invalid):
                self.config["limits"]["max_model_calls"] = invalid
                transport = SequenceTransport([])
                with self.assertRaises(ValueError):
                    run_analysis(self.config, transport=transport)
                self.assertEqual(transport.requests, [])

    def test_unknown_citation_never_becomes_completed_report(self) -> None:
        result = run_analysis(self.config, transport=SequenceTransport([read_call("average.py"),
                                                                       final_response("E999"), final_response("E999")]))
        self.assertEqual(result.status, "system_failure")
        self.assertIsNone(result.report)
        self.assertIn("unknown_evidence", result.reason)
        self.assertEqual(len(result.evidence), 2)
        self.assertIn("尚未形成有效", result.markdown)

    def test_citation_cannot_expand_the_actual_read_range(self) -> None:
        result = run_analysis(self.config, transport=SequenceTransport([read_call("average.py", 1, 1),
                                                                       final_response(),final_response()]))
        self.assertEqual(result.status, "system_failure")
        self.assertIn("invalid_reference_range", result.reason)

    def test_last_model_slot_can_finish_but_cannot_start_another_tool(self) -> None:
        self.config["limits"]["max_model_calls"] = 2
        completed = run_analysis(self.config, transport=SequenceTransport([read_call("average.py"),
                                                                          final_response()]))
        self.assertEqual(completed.status, "completed")
        stopped = run_analysis(self.config, transport=SequenceTransport([read_call("average.py"),
                                                                        read_call("average.py")]))
        self.assertEqual(stopped.status, "limits_exhausted")
        self.assertEqual(stopped.model_calls, 2)
        self.assertEqual(stopped.tool_calls, 1)
        self.assertEqual(len(stopped.evidence), 2)

    def test_rejected_tool_consumes_budget_without_bonus_final_model_call(self) -> None:
        self.config["limits"]["max_tool_calls"] = 1
        transport = SequenceTransport([read_call("does-not-exist.py")])
        result = run_analysis(self.config, transport=transport)
        self.assertEqual(result.status, "limits_exhausted")
        self.assertEqual((result.model_calls, result.tool_calls), (1, 1))
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(result.evidence[0].kind, "initial_material")
        self.assertTrue(any(event.detail == "file_unavailable" for event in result.events))

    def test_oversized_slice_is_not_stored_as_evidence(self) -> None:
        self.config["limits"].update({"max_read_bytes": 8, "max_tool_calls": 1})
        result = run_analysis(self.config, transport=SequenceTransport([read_call("average.py")]))
        self.assertEqual([item.kind for item in result.evidence], ["initial_material"])
        self.assertTrue(any(event.detail == "read_limit" for event in result.events))

    def test_total_timeout_prevents_tool_execution_after_late_model_response(self) -> None:
        class SlowTransport(SequenceTransport):
            def complete(self, request: dict[str, Any], control: RunControl) -> dict[str, Any]:
                control.wait(0.03)
                return super().complete(request, control)

        self.config["limits"]["total_seconds"] = 0.01
        result = run_analysis(self.config, transport=SlowTransport([read_call("average.py")]))
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.tool_calls, 0)
        self.assertIsNone(result.report)

    def test_external_error_text_is_not_written_to_artifacts(self) -> None:
        class FailingTransport:
            def complete(self, request: dict[str, Any], control: RunControl) -> dict[str, Any]:
                raise RuntimeError("not-a-real-credential")

        result = run_analysis(self.config, transport=FailingTransport())
        output = Path(self.temp.name) / "failure"
        result.write_to(output)
        self.assertEqual(result.status, "system_failure")
        self.assertNotIn("not-a-real-credential", (output / "run.json").read_text(encoding="utf-8"))
        self.assertNotIn("not-a-real-credential", result.markdown)

    def test_report_output_cannot_overwrite_authorized_material(self) -> None:
        result = run_analysis(self.config, transport=SequenceTransport([read_call("average.py"), final_response()]))
        with self.assertRaises(ValueError):
            result.write_to(self.root)
        self.assertFalse((self.root / "run.json").exists())

    def test_cyclic_config_paths_are_rejected_as_configuration_errors(self) -> None:
        config_loop = Path(self.temp.name) / "config-loop.json"
        config_loop.symlink_to("config-loop.json")
        directory_loop = self.root / "directory-loop"
        directory_loop.symlink_to("directory-loop")
        for source in (config_loop, {**self.config, "authorized_dir": str(directory_loop)}):
            with self.subTest(source_type=type(source).__name__):
                transport = SequenceTransport([])
                with self.assertRaisesRegex(ValueError, "路径"):
                    run_analysis(source, transport=transport)
                self.assertEqual(transport.requests, [])

    def test_correct_response(self) -> None:
        transport = SequenceTransport([
        read_call("average.py"),
        final_response("E999"),  # 首份报告无效
        final_response("E2"),    # 修正后引用有效
    ])
        result = run_analysis(self.config, transport=transport)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.model_calls, 3)
        self.assertEqual(result.tool_calls, 1)

        feedback = json.loads(transport.requests[2]["messages"][-1]["content"])
        self.assertEqual(feedback["error"], "unknown_evidence")

    def test_run_out_of_limit(self) -> None:
        self.config["limits"]["max_model_calls"] = 2
        transport = SequenceTransport([
        read_call("average.py"),
        final_response("E999"),  # 首份报告无效
        final_response("E2"),    # 修正后引用有效
    ])
        result = run_analysis(self.config, transport=transport)
        self.assertEqual(result.status, "limits_exhausted")
        self.assertEqual(result.model_calls, 2)
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(len(transport.responses), 1)  # 第三份回复仍未被取用


if __name__ == "__main__":
    unittest.main()
