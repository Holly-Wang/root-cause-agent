import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
import tempfile
import unittest
from pathlib import Path
from typing import Any

from rca_agent import CancellationToken, RunControl, run_analysis
from rca_agent.config import ConfigError
from tests.test_discovery import ScriptedModel, conclusion, tool_call


def ask() -> dict[str, Any]:
    return tool_call("ask_user", {"gap": "缺少实际输入及运行结果。", "question": "请提供实际输入和结果。"}, "ask-1")


def gap_report() -> dict[str, Any]:
    report = {
        "root_cause_found": False, "candidate_causes": [], "verification": [],
        "information_gaps": ["缺少失败调用的实际输入及输出。"],
        "follow_up_questions": ["失败调用使用什么输入，实际得到什么输出？"],
        "unconfirmed": ["尚未找到或确认根因。"], "repair_suggestions": [],
        "next_steps": ["取得可复现输入后继续调查。"],
    }
    return {"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": json.dumps(report, ensure_ascii=False),
    }}]}


class Replies:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.requests: list[Any] = []
        self.controls: list[RunControl] = []

    def receive(self, request: Any, control: RunControl) -> Any:
        control.checkpoint()
        self.requests.append(request)
        self.controls.append(control)
        return self.answers.pop(0)


class SupplementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "material"
        self.root.mkdir()
        (self.root / "source.py").write_text("def average(values):\n    return sum(values) / 2\n", encoding="utf-8")
        self.config: dict[str, Any] = {
            "question": "调查平均值", "initial_materials": ["用户最初提供的现象"],
            "authorized_dir": str(self.root), "model": "deepseek-flash",
            "limits": {"total_seconds": 5, "max_model_calls": 10, "max_tool_calls": 10},
        }

    def test_text_reply_continues_the_same_investigation_as_unverified_user_evidence(self) -> None:
        model = ScriptedModel([
            tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2}),
            ask(), conclusion("E2", ("E3",)),
        ])
        replies = Replies({"kind": "text", "text": "我在本机测试通过，输入是三项。"})
        result = run_analysis(self.config, transport=model, input_provider=replies)
        self.assertEqual(result.status, "completed", result.reason)
        self.assertEqual((result.model_calls, result.tool_calls, len(model.requests)), (3, 2, 3))
        self.assertEqual([e.kind for e in result.evidence], ["initial_material", "file", "user_supplement"])
        self.assertEqual(result.evidence[2].content, "我在本机测试通过，输入是三项。")
        feedback = json.loads(model.requests[2]["messages"][-1]["content"])
        self.assertEqual(model.requests[2]["messages"][-1]["tool_call_id"], "ask-1")
        self.assertEqual(feedback["provenance"], "user_provided_unverified")
        self.assertEqual(feedback["evidence"]["id"], "E3")
        self.assertEqual(replies.requests[0].question, "请提供实际输入和结果。")
        self.assertIn("未经本次宿主测试验证", result.markdown)

    def test_file_reply_is_really_read_and_keeps_the_original_authorization(self) -> None:
        replies = Replies({"kind": "file", "path": "source.py", "start_line": 1, "end_line": 2})
        model = ScriptedModel([ask(), conclusion("E2")])
        result = run_analysis(self.config, transport=model, input_provider=replies)
        self.assertEqual(result.status, "completed", result.reason)
        self.assertEqual(result.evidence[1].kind, "file")
        self.assertEqual(result.evidence[1].source, "source.py")
        self.assertIn("return sum(values) / 2", result.evidence[1].content)
        self.assertEqual(result.config_snapshot["authorized_dir"], str(self.root.resolve()))
        self.assertEqual((result.model_calls, result.tool_calls), (2, 1))
        self.assertEqual(json.loads(model.feedback()[0]["content"])["evidence"]["end_line"], 2)

    def test_invalid_and_external_answers_are_rejected_inside_the_same_wait(self) -> None:
        outside = Path(self.temp.name) / "private.py"
        outside.write_text("PRIVATE-CONTENT", encoding="utf-8")
        (self.root / "link.py").symlink_to(outside)
        self.config["limits"]["max_supplement_attempts"] = 5
        replies = Replies(
            {"kind": "text", "text": "   "},
            {"kind": "file", "path": str(outside), "start_line": 1, "end_line": 1},
            {"kind": "file", "path": "link.py", "start_line": 1, "end_line": 1},
            {"kind": "test", "test_id": "new-test"},
            {"kind": "file", "path": "source.py", "start_line": 1, "end_line": 2},
        )
        model = ScriptedModel([ask(), conclusion("E2")])
        result = run_analysis(self.config, transport=model, input_provider=replies)
        self.assertEqual(result.status, "completed", result.reason)
        self.assertEqual((len(replies.requests), len(model.requests), result.tool_calls), (5, 2, 1))
        self.assertEqual(len({id(c) for c in replies.controls}), 1)
        self.assertEqual(len({c.deadline for c in replies.controls}), 1)
        self.assertEqual(sum(e.kind == "input_rejected" for e in result.events), 4)
        self.assertEqual(replies.requests[2].error, "permission_denied")
        self.assertNotIn("PRIVATE-CONTENT", result.markdown)
        self.assertIn("修改配置重新启动", " ".join(e.detail for e in result.events))

    def test_honest_insufficient_report_can_complete_without_inventing_file_evidence(self) -> None:
        model = ScriptedModel([gap_report()])
        result = run_analysis(self.config, transport=model)
        self.assertEqual((result.status, result.reason), ("completed", "valid_report"))
        assert result.report is not None
        self.assertIs(result.report["root_cause_found"], False)
        self.assertEqual(result.report["candidate_causes"], [])
        self.assertIn("尚未找到或确认根因", result.markdown)
        self.assertIn("缺少失败调用的实际输入", result.markdown)
        self.assertIn("实际得到什么输出", result.markdown)
        self.assertEqual((result.model_calls, result.tool_calls), (1, 0))

    def test_contradictory_found_status_is_not_accepted_as_a_valid_report(self) -> None:
        invalid = conclusion("E2")
        content = json.loads(invalid["choices"][0]["message"]["content"])
        content["root_cause_found"] = True
        invalid["choices"][0]["message"]["content"] = json.dumps(content)
        model = ScriptedModel([
            tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2}), invalid, invalid,
        ])
        result = run_analysis(self.config, transport=model)
        self.assertEqual(result.status, "system_failure")
        self.assertIn("inconsistent_diagnosis_status", result.reason)
        self.assertIsNone(result.report)

    def test_user_claim_cannot_be_cited_as_host_verification(self) -> None:
        invalid = gap_report()
        content = json.loads(invalid["choices"][0]["message"]["content"])
        content["verification"] = [{"summary": "宿主已测试通过", "evidence": [{"evidence_id": "E2"}]}]
        invalid["choices"][0]["message"]["content"] = json.dumps(content)
        model = ScriptedModel([ask(), invalid, invalid])
        result = run_analysis(self.config, transport=model,
                              input_provider=Replies({"kind": "text", "text": "我本机测试通过。"}))
        self.assertEqual(result.status, "system_failure")
        self.assertIn("unverified_user_evidence", result.reason)
        self.assertNotIn("宿主已测试通过", result.markdown)
        self.assertIn("我本机测试通过", result.markdown)

    def test_decline_eof_and_unavailable_input_can_return_an_honest_gap_summary(self) -> None:
        for replies in (Replies({"kind": "decline"}), Replies(None), None):
            with self.subTest(provider=replies):
                model = ScriptedModel([ask(), gap_report()])
                result = run_analysis(self.config, transport=model, input_provider=replies)
                self.assertEqual(result.status, "completed", result.reason)
                self.assertEqual(len(result.evidence), 1)
                feedback = json.loads(model.requests[1]["messages"][-1]["content"])
                self.assertTrue(feedback["declined"])
                self.assertEqual((result.model_calls, result.tool_calls), (2, 1))

    def test_supplement_file_read_limit_stops_and_keeps_previous_evidence(self) -> None:
        self.config["limits"]["max_read_bytes"] = 1
        model = ScriptedModel([ask(), gap_report()])
        replies = Replies({"kind": "file", "path": "source.py", "start_line": 1, "end_line": 2}, {"kind": "decline"})
        result = run_analysis(self.config, transport=model, input_provider=replies)
        self.assertEqual((result.status, result.reason), ("limits_exhausted", "max_read_bytes"))
        self.assertEqual((len(replies.requests), len(model.requests), len(result.evidence)), (1, 1, 1))

    def test_explicit_input_cancel_stops_without_a_summary_request(self) -> None:
        model = ScriptedModel([ask(), gap_report()])
        result = run_analysis(self.config, transport=model, input_provider=Replies({"kind": "cancel"}))
        self.assertEqual((result.status, result.reason), ("user_cancelled", "user_cancelled"))
        self.assertEqual((result.model_calls, result.tool_calls, len(model.requests)), (1, 1, 1))

    def test_oversized_reply_stops_without_saving_the_rejected_content(self) -> None:
        self.config["limits"]["max_supplement_bytes"] = 128
        model = ScriptedModel([ask(), gap_report()])
        result = run_analysis(self.config, transport=model,
                              input_provider=Replies({"kind": "text", "text": "拒绝的原文" * 100}))
        self.assertEqual((result.status, result.reason), ("limits_exhausted", "max_supplement_bytes"))
        self.assertEqual((result.model_calls, result.tool_calls, len(model.requests)), (1, 1, 1))
        self.assertEqual(len(result.evidence), 1)
        self.assertNotIn("拒绝的原文", result.markdown)
        self.assertFalse(any("拒绝的原文" in e.detail for e in result.events))

    def test_oversized_question_is_rejected_before_waiting_or_echoing_it(self) -> None:
        self.config["limits"]["max_tool_feedback_bytes"] = 2048
        question = tool_call("ask_user", {"gap": "缺口" * 1000, "question": "请提供材料"})
        replies = Replies({"kind": "decline"})
        model = ScriptedModel([question, gap_report()])
        result = run_analysis(self.config, transport=model, input_provider=replies)
        self.assertEqual((result.status, result.reason), ("limits_exhausted", "max_tool_feedback_bytes"))
        self.assertEqual(replies.requests, [])
        self.assertFalse(any(e.kind == "waiting_for_user" for e in result.events))

    def test_waiting_uses_the_fixed_deadline_and_cancellation_without_model_calls(self) -> None:
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                ready, closed = threading.Event(), threading.Event()
                cancellation = CancellationToken()
                model = ScriptedModel([
                    tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2}), ask(), gap_report(),
                ])
                self.config["limits"]["total_seconds"] = 5 if cancel else 0.15

                class WaitingInput:
                    def receive(inner, request: Any, control: RunControl) -> Any:
                        ready.set()
                        try:
                            self.assertEqual(len(model.requests), 2)
                            control.wait()
                        finally:
                            closed.set()

                def cancel_when_ready() -> None:
                    if ready.wait(3):
                        self.assertEqual(len(model.requests), 2)
                        cancellation.cancel()

                controller = threading.Thread(target=cancel_when_ready)
                if cancel:
                    controller.start()
                try:
                    result = run_analysis(self.config, transport=model, input_provider=WaitingInput(), cancellation=cancellation)
                finally:
                    if cancel:
                        controller.join(4)
                expected = ("user_cancelled", "user_cancelled") if cancel else ("timeout", "total_deadline")
                self.assertEqual((result.status, result.reason), expected)
                self.assertTrue(ready.is_set() and closed.is_set())
                self.assertEqual((result.model_calls, result.tool_calls, len(model.requests)), (2, 2, 2))
                self.assertEqual([e.kind for e in result.evidence], ["initial_material", "file"])
                self.assertIn("缺少实际输入及运行结果", result.markdown)

    def start_cli(self) -> tuple[subprocess.Popen[bytes], Path, bytes]:
        bootstrap = Path(self.temp.name) / "bootstrap"
        bootstrap.mkdir()
        (bootstrap / "sitecustomize.py").write_text(
            "import rca_agent.runtime as runtime\n"
            "from tests.test_discovery import ScriptedModel,tool_call\n"
            "from tests.test_supplement import ask,gap_report\n"
            "runtime.DeepSeekTransport=lambda:ScriptedModel([\n"
            "tool_call('read_file',{'path':'source.py','start_line':1,'end_line':2}),ask(),gap_report()])\n",
            encoding="utf-8")
        config = Path(self.temp.name) / "config.json"
        config.write_text(json.dumps(self.config), encoding="utf-8")
        output = Path(self.temp.name) / "cli-output"
        env = {**os.environ, "PYTHONPATH": str(bootstrap) + os.pathsep + str(Path.cwd())}
        process = subprocess.Popen([sys.executable, "-m", "rca_agent", "--config", str(config),
                                    "--output", str(output)], env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)

        def reap() -> None:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=4)

        self.addCleanup(reap)
        assert process.stderr is not None
        captured = b""
        until = time.monotonic() + 4
        while b"[waiting_for_user]" not in captured and time.monotonic() < until:
            if select.select([process.stderr], [], [], 0.1)[0]:
                chunk = os.read(process.stderr.fileno(), 4096)
                if not chunk:
                    break
                captured += chunk
        self.assertIn(b"[waiting_for_user]", captured, captured.decode(errors="replace"))
        return process, output, captured

    def test_cli_consumes_real_stdin_in_the_same_run(self) -> None:
        process, output, progress = self.start_cli()
        stdout, stderr = process.communicate("我在本机测试通过\n".encode("utf-8"), timeout=5)
        self.assertEqual(process.returncode, 0, (progress + stderr).decode())
        saved = json.loads((output / "run.json").read_text())
        self.assertEqual(saved["status"], "completed")
        self.assertEqual([e["kind"] for e in saved["evidence"]], ["initial_material", "file", "user_supplement"])
        self.assertEqual(saved["evidence"][2]["content"], "我在本机测试通过")
        self.assertEqual((saved["model_calls"], saved["tool_calls"]), (3, 2))

    def test_cli_sigint_interrupts_stdin_wait_without_a_final_model_call(self) -> None:
        process, output, progress = self.start_cli()
        process.send_signal(signal.SIGINT)
        process.wait(timeout=4)  # 保持 stdin 打开，不能用 EOF 假装取消等待。
        process.communicate(timeout=2)
        saved = json.loads((output / "run.json").read_text())
        self.assertEqual((saved["status"], saved["reason"]), ("user_cancelled", "user_cancelled"))
        self.assertEqual((saved["model_calls"], saved["tool_calls"]), (2, 2))
        self.assertEqual([e["kind"] for e in saved["evidence"]], ["initial_material", "file"])

    def test_cli_no_input_deadline_does_not_depend_on_eof(self) -> None:
        self.config["limits"]["total_seconds"] = 0.5
        process, output, progress = self.start_cli()
        process.wait(timeout=4)
        process.communicate(timeout=2)
        saved = json.loads((output / "run.json").read_text())
        self.assertEqual((saved["status"], saved["reason"]), ("timeout", "total_deadline"))
        self.assertEqual((saved["model_calls"], saved["tool_calls"]), (2, 2))
        self.assertGreaterEqual(saved["elapsed_seconds"], 0.5)
        self.assertIn("return sum(values) / 2", (output / "report.md").read_text())

    def test_cli_empty_input_then_real_file_reference_and_eof_summary(self) -> None:
        process, output, progress = self.start_cli()
        process.communicate(b'\n{"kind":"file","path":"source.py","start_line":1,"end_line":2}\n', timeout=4)
        saved = json.loads((output / "run.json").read_text())
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["evidence"][2]["kind"], "file")
        self.assertEqual(sum(e["kind"] == "input_rejected" for e in saved["events"]), 1)
        self.assertEqual((saved["model_calls"], saved["tool_calls"]), (3, 2))

    def test_cli_eof_is_a_decline_with_an_honest_summary(self) -> None:
        process, output, progress = self.start_cli()
        process.communicate(b"", timeout=4)
        saved = json.loads((output / "run.json").read_text())
        self.assertEqual(saved["status"], "completed")
        self.assertFalse(saved["report"]["root_cause_found"])
        self.assertEqual(len(saved["evidence"]), 2)

    def test_supplement_respects_cumulative_evidence_and_full_context_budgets(self) -> None:
        answer = {"kind": "text", "text": "新的用户材料" * 100}
        for resource in ("max_evidence_bytes", "max_context_bytes"):
            with self.subTest(resource=resource):
                self.config["limits"].pop("max_context_bytes", None)
                self.config["limits"].pop("max_evidence_bytes", None)
                baseline = ScriptedModel([ask(), gap_report()])
                run_analysis(self.config, transport=baseline, input_provider=Replies({"kind": "decline"}))
                if resource == "max_context_bytes":
                    from tests.test_resource_limits import wire_size
                    before_reply = json.loads(json.dumps(baseline.requests[0]))
                    before_reply["messages"].append(ask()["choices"][0]["message"])
                    self.config["limits"][resource] = wire_size(before_reply) + 100
                else:
                    self.config["limits"][resource] = 250
                model = ScriptedModel([ask(), gap_report()])
                result = run_analysis(self.config, transport=model, input_provider=Replies(answer))
                self.assertEqual((result.status, result.reason), ("limits_exhausted", resource))
                self.assertEqual((result.model_calls, result.tool_calls, len(model.requests)), (1, 1, 1))
                self.assertEqual(len(result.evidence), 2 if resource == "max_context_bytes" else 1)
                output = Path(self.temp.name) / resource
                result.write_to(output)
                saved = json.loads((output / "run.json").read_text())
                self.assertEqual(saved["reason"], resource)
                self.assertEqual(saved["evidence"][0]["content"], "用户最初提供的现象")
                if resource == "max_evidence_bytes":
                    self.assertNotIn("新的用户材料", (output / "run.json").read_text())

    def test_invalid_answers_exhaust_only_the_same_wait_attempt_budget(self) -> None:
        self.config["limits"]["max_supplement_attempts"] = 2
        replies = Replies({"kind": "text", "text": ""}, {"kind": "file", "path": "../outside"}, {"kind": "decline"})
        model = ScriptedModel([ask(), gap_report()])
        result = run_analysis(self.config, transport=model, input_provider=replies)
        self.assertEqual((result.status, result.reason), ("limits_exhausted", "max_supplement_attempts"))
        self.assertEqual((len(replies.requests), len(model.requests), result.tool_calls), (2, 1, 1))
        self.assertEqual(len(result.evidence), 1)

    def test_report_repair_and_tool_counts_do_not_reset_after_a_reply(self) -> None:
        invalid = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "{}"}}]}
        model = ScriptedModel([invalid, ask(), invalid, gap_report()])
        result = run_analysis(self.config, transport=model, input_provider=Replies({"kind": "text", "text": "仍缺复现输入"}))
        self.assertEqual(result.status, "system_failure")
        self.assertEqual((result.model_calls, result.tool_calls, len(model.requests)), (3, 1, 3))
        self.assertEqual(sum(e.kind == "report_validation_feedback" for e in result.events), 1)
        self.assertEqual(len(result.evidence), 2)
        self.config["limits"]["max_tool_calls"] = 1
        model = ScriptedModel([ask(), gap_report()])
        limited = run_analysis(self.config, transport=model, input_provider=Replies({"kind": "text", "text": "补充材料"}))
        self.assertEqual((limited.status, limited.reason), ("limits_exhausted", "call_limit"))
        self.assertEqual((limited.model_calls, limited.tool_calls, len(model.requests)), (1, 1, 1))
        self.assertEqual(len(limited.evidence), 2)

    def test_invalid_status_fields_legacy_format_and_forged_citations_are_rejected(self) -> None:
        base = json.loads(gap_report()["choices"][0]["message"]["content"])
        cases: list[dict[str, Any]] = [
            {"root_cause_found": 1}, {"root_cause_found": "false"}, {"root_cause_found": True},
            {"information_gaps": []}, {"follow_up_questions": []}, {"unconfirmed": []},
            {"verification": [{"summary": "虚构观察", "evidence": [{"evidence_id": "E999"}]}]},
            {"candidate_causes": [{"summary": "伪造文件依据", "supporting_evidence": [{"evidence_id": "E1"}],
                                   "contradicting_evidence": []}]},
        ]
        reports = [{**base, **fields} for fields in cases]
        reports.append({key: value for key, value in base.items()
                        if key not in ("root_cause_found", "information_gaps", "follow_up_questions")})
        for report in reports:
            with self.subTest(report=report):
                response = {"choices": [{"finish_reason": "stop", "message": {
                    "role": "assistant", "content": json.dumps(report),
                }}]}
                model = ScriptedModel([response, response])
                result = run_analysis(self.config, transport=model)
                self.assertEqual(result.status, "system_failure")
                self.assertIsNone(result.report)
                self.assertEqual(result.model_calls, 2)

    def test_found_flag_is_a_model_claim_not_host_proof(self) -> None:
        response = conclusion("E2")
        report = json.loads(response["choices"][0]["message"]["content"])
        report.update({"root_cause_found": True, "information_gaps": []})
        response["choices"][0]["message"]["content"] = json.dumps(report)
        model = ScriptedModel([tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2}), response])
        result = run_analysis(self.config, transport=model)
        self.assertEqual(result.status, "completed", result.reason)
        self.assertIn("模型声明已找到根因；宿主未证明诊断正确", result.markdown)

    def test_single_reply_maximum_size_is_inclusive_and_configuration_is_strict(self) -> None:
        # {"kind":"text","text":"你好"} 稳定 JSON 为 31 个 UTF-8 字节。
        for budget, expected in ((31, "completed"), (30, "limits_exhausted")):
            self.config["limits"]["max_supplement_bytes"] = budget
            model = ScriptedModel([ask(), gap_report()])
            result = run_analysis(self.config, transport=model, input_provider=Replies({"kind": "text", "text": "你好"}))
            self.assertEqual(result.status, expected, result.reason)
        self.config["limits"].pop("max_supplement_bytes")
        for field in ("max_supplement_bytes", "max_supplement_attempts"):
            for invalid in (True, 0, -1, 1.2, "30"):
                self.config["limits"][field] = invalid
                with self.assertRaisesRegex(ConfigError, field):
                    run_analysis(self.config, transport=ScriptedModel([]))
            self.config["limits"].pop(field)

    def test_cli_unterminated_oversized_input_is_bounded_without_waiting_for_eof(self) -> None:
        self.config["limits"]["max_supplement_bytes"] = 128
        process, output, progress = self.start_cli()
        assert process.stdin is not None
        process.stdin.write(b"REJECTED-RAW-" * 20)
        process.stdin.flush()
        process.wait(timeout=3)
        process.communicate(timeout=2)
        text = (output / "run.json").read_text()
        saved = json.loads(text)
        self.assertEqual((saved["status"], saved["reason"]), ("limits_exhausted", "max_supplement_bytes"))
        self.assertEqual((saved["model_calls"], saved["tool_calls"]), (2, 2))
        self.assertNotIn("REJECTED-RAW-", text)

    def test_supplement_does_not_expand_permissions_or_replace_the_run_control(self) -> None:
        self.config["permitted_tests"] = {"allowed": {
            "command": [sys.executable, "-c", "print('ORIGINAL')"], "timeout_seconds": 2,
        }}
        controls: list[RunControl] = []

        class ObservedModel(ScriptedModel):
            def complete(inner, request: dict[str, Any], control: RunControl) -> dict[str, Any]:
                controls.append(control)
                return super().complete(request, control)

        config = self.config

        class ChangedInput(Replies):
            def receive(inner, request: Any, control: RunControl) -> Any:
                config["permitted_tests"]["allowed"]["command"][-1] = "print('CHANGED')"
                config["permitted_tests"]["new"] = config["permitted_tests"]["allowed"]
                config["limits"]["total_seconds"] = 999
                return super().receive(request, control)

        provider = ChangedInput({"kind": "text", "text": "请求执行新的测试。"})
        model = ObservedModel([
            tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2}), ask(),
            tool_call("run_test", {"test_id": "new"}, "denied"),
            tool_call("run_test", {"test_id": "allowed"}, "allowed"), conclusion("E2", ("E3", "E4")),
        ])
        result = run_analysis(self.config, transport=model, input_provider=provider)
        self.assertEqual(result.status, "completed", result.reason)
        self.assertEqual((result.model_calls, result.tool_calls), (5, 4))
        self.assertTrue(all(c is provider.controls[0] for c in controls))
        self.assertEqual(result.config_snapshot["limits"]["total_seconds"], 5)
        self.assertEqual(set(result.config_snapshot["permitted_tests"]), {"allowed"})
        self.assertEqual(json.loads(result.evidence[3].content)["output"], "ORIGINAL\n")
        denied = next(m for m in model.requests[-1]["messages"] if m.get("tool_call_id") == "denied")
        self.assertEqual(json.loads(denied["content"])["classification"], "permission_denied")

    def test_control_replies_are_not_material_and_cancel_wins_even_with_a_tiny_reply_budget(self) -> None:
        self.config["limits"]["max_supplement_bytes"] = 1
        for answer, expected in (({"kind": "cancel"}, "user_cancelled"), (None, "completed")):
            with self.subTest(answer=answer):
                model = ScriptedModel([ask(), gap_report()])
                result = run_analysis(self.config, transport=model, input_provider=Replies(answer))
                self.assertEqual(result.status, expected, result.reason)
                self.assertEqual(len(result.evidence), 1)
