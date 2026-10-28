import json
import signal
import os
import time
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from rca_agent import run_analysis
from rca_agent.config import ConfigError
from rca_agent.report import RunResult
from tests.test_discovery import ScriptedModel, conclusion, tool_call


class PermittedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "material"
        self.root.mkdir()
        (self.root / "source.py").write_text("value = 1\nvalue += 1\n", encoding="utf-8")
        self.config: dict[str, Any] = {
            "question": "验证候选原因", "initial_materials": [],
            "authorized_dir": str(self.root), "model": "deepseek-flash",
            "limits": {"total_seconds": 10}, "permitted_tests": {},
        }

    def permit(self, code: str, **options: Any) -> None:
        (self.root / "check.py").write_text(code, encoding="utf-8")
        self.config["permitted_tests"]["check"] = {
            "command": [sys.executable, "check.py"], "timeout_seconds": 2,
            **options,
        }

    def investigate(self) -> tuple[RunResult, ScriptedModel]:
        def report(request: dict[str, Any]) -> dict[str, Any]:
            feedback = json.loads(request["messages"][-1]["content"])
            test_result = json.loads(feedback["evidence"]["content"])
            response = conclusion("E1", ("E2",))
            content = json.loads(response["choices"][0]["message"]["content"])
            summary = ("测试自身超时，进程组已清理；测试验证未完成。"
                       if test_result["classification"] == "timeout" else "实际测试观察")
            content["verification"] = [{"summary": summary, "evidence": [{"evidence_id": "E2"}]}]
            content["unconfirmed"] = ["测试观察不足以确认候选根因。"]
            content["information_gaps"] = ["测试观察与候选根因间的因果联系仍未确认。"]
            response["choices"][0]["message"]["content"] = json.dumps(content)
            return response

        model = ScriptedModel([
            tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2}, "read"),
            tool_call("run_test", {"test_id": "check"}, "test"), report,
        ])
        return run_analysis(self.config, transport=model), model

    def test_success_is_returned_and_citable(self) -> None:
        self.permit("print('REAL_SUCCESS')\n")
        result, model = self.investigate()
        self.assertEqual(result.status, "completed", result.reason)
        item = result.evidence[1]
        self.assertEqual((item.kind, item.source), ("test", "check"))
        data = json.loads(item.content)
        self.assertEqual(data["test_id"], "check")
        self.assertEqual(data["output"], "REAL_SUCCESS\n")
        self.assertEqual(data["exit_code"], 0)
        self.assertIsNone(data["signal"])
        self.assertEqual(data["classification"], "passed")
        self.assertEqual(data["cleanup_status"], "confirmed")
        self.assertFalse(data["output_truncated"])
        feedback = json.loads(model.feedback()[-1]["content"])
        self.assertEqual(feedback["evidence"]["content"], item.content)
        self.assertIn("宿主许可测试", result.markdown)
        self.assertIn("操作系统沙箱", result.markdown)
        self.assertIn("run_test", {t["function"]["name"] for t in model.requests[0]["tools"]})
        output = Path(self.temp.name) / "saved"
        result.write_to(output)
        saved = json.loads((output / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["config_snapshot"]["permitted_tests"]["check"]["command"][1], "check.py")
        self.assertTrue(any(e.kind == "test_result" and "test" in e.detail for e in result.events))

    def test_only_declared_assertion_exit_codes_count_as_assertion_failures(self) -> None:
        cases = [
            ("try:\n    assert 2 == 3\nexcept AssertionError:\n    print('ASSERTION_FAILED')\n    raise SystemExit(10)\n",
             [10], "assertion_failed", 10, None, "configured_exit_code"),
            ("print('AssertionError is merely text')\nraise SystemExit(1)\n",
             [], "execution_error", 1, None, "unclassified_nonzero_exit"),
            ("import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n",
             [10], "execution_error", None, signal.SIGTERM, "signal"),
        ]
        for code, codes, classification, exit_code, sig, basis in cases:
            with self.subTest(classification=classification, exit_code=exit_code, signal=sig):
                self.permit(code, assertion_failure_exit_codes=codes)
                result, _ = self.investigate()
                self.assertEqual(result.status, "completed", result.reason)
                data = json.loads(result.evidence[1].content)
                self.assertEqual(data["classification"], classification)
                self.assertEqual(data["classification_basis"], basis)
                self.assertEqual(data["exit_code"], exit_code)
                self.assertEqual(data["signal"], sig)
                self.assertIn(classification, result.markdown)

    def test_start_failure_has_no_fabricated_exit_status(self) -> None:
        self.permit("print('SHOULD_NOT_RUN')\n")
        program = self.root / "invalid-program"
        program.write_text("not an executable format\n", encoding="utf-8")
        program.chmod(0o700)
        self.config["permitted_tests"]["check"]["command"] = [str(program)]
        result, model = self.investigate()
        self.assertEqual(result.status, "completed", result.reason)
        data = json.loads(result.evidence[1].content)
        self.assertEqual(data["classification"], "execution_error")
        self.assertEqual(data["classification_basis"], "start_failed")
        self.assertFalse(data["started"])
        self.assertIsNone(data["exit_code"])
        self.assertIsNone(data["signal"])
        self.assertEqual(data["cleanup_status"], "not_started")
        self.assertEqual(data["output"], "")
        self.assertIn("start_failed", model.feedback()[-1]["content"])

    def test_unpermitted_or_modified_calls_are_rejected_without_starting(self) -> None:
        self.permit("from pathlib import Path\nPath('started').touch()\n")
        for arguments, error in [
            ({"test_id": "unknown"}, "permission_denied"),
            ({"test_id": "check", "command": [sys.executable, "check.py"]}, "invalid_arguments"),
            ({"test_id": "check", "args": ["extra"]}, "invalid_arguments"),
            ({"test_id": 123}, "invalid_arguments"),
        ]:
            with self.subTest(arguments=arguments):
                model = ScriptedModel([
                    tool_call("run_test", arguments, "denied"),
                    tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2}),
                    conclusion("E1"),
                ])
                result = run_analysis(self.config, transport=model)
                self.assertEqual(result.status, "completed", result.reason)
                self.assertFalse((self.root / "started").exists())
                self.assertEqual(len(result.evidence), 1)
                feedback = json.loads(model.feedback()[0]["content"])
                self.assertEqual(feedback["error"], error)
                self.assertEqual(feedback["classification"], "permission_denied" if error == "permission_denied" else "invalid_request")
                self.assertFalse(feedback["started"])
                self.assertIsNone(feedback["exit_code"])
                self.assertIsNone(feedback["signal"])
                self.assertEqual(feedback["cleanup_status"], "not_started")
                self.assertIn("denied", result.markdown)

    def test_output_is_bounded_including_feedback_json_escaping(self) -> None:
        self.permit("import os\nfor _ in range(256):\n    os.write(1, b'\\x00\\xff\\\"\\\\' * 2048)\n    os.write(2, b'ERR' * 2048)\n")
        self.config["limits"].update(max_test_output_bytes=4096, max_tool_feedback_bytes=2048)
        result, model = self.investigate()
        self.assertEqual(result.status, "completed", result.reason)
        data = json.loads(result.evidence[1].content)
        self.assertEqual(data["exit_code"], 0)
        self.assertTrue(data["output_truncated"])
        self.assertLessEqual(len(data["output"].encode("utf-8")), 4096)
        self.assertLessEqual(len(model.feedback()[-1]["content"].encode("utf-8")), 2048)
        self.assertIn('"output_truncated": true', result.markdown)

    def assert_timeout_returned_to_model(self, result: RunResult, model: ScriptedModel) -> None:
        self.assertEqual((result.status, result.reason), ("completed", "valid_report"))
        self.assertEqual(len(model.requests), 3)
        self.assertEqual((result.model_calls, result.tool_calls), (3, 2))
        messages = model.requests[2]["messages"]
        self.assertEqual(messages[-1]["role"], "tool")
        self.assertEqual(messages[-1]["tool_call_id"], "test")
        self.assertEqual(messages[-2]["tool_calls"][0]["id"], "test")
        self.assertEqual(messages[-2]["tool_calls"][0]["function"]["name"], "run_test")
        feedback = json.loads(messages[-1]["content"])
        self.assertTrue(feedback["ok"])
        self.assertEqual(feedback["evidence"]["id"], "E2")
        self.assertEqual(feedback["evidence"]["content"], result.evidence[1].content)
        data = json.loads(feedback["evidence"]["content"])
        self.assertEqual(data["test_id"], "check")
        self.assertEqual(data["classification"], "timeout")
        self.assertEqual(data["cleanup_status"], "confirmed")
        if result.report is None:
            self.fail("超时证据回填后应形成有效报告。")
        self.assertEqual(result.report["verification"], [{
            "summary": "测试自身超时，进程组已清理；测试验证未完成。",
            "evidence": [{"evidence_id": "E2"}],
        }])
        self.assertIn("测试自身超时，进程组已清理；测试验证未完成。", result.markdown)
        self.assertIn('"classification": "timeout"', result.markdown)

    def test_test_timeout_returns_to_model_after_cleanup(self) -> None:
        self.permit("import os, time\nprint(os.getpgrp(), flush=True)\ntime.sleep(0.6)\n",
                    timeout_seconds=0.15)
        started = time.monotonic()
        result, model = self.investigate()
        self.assert_timeout_returned_to_model(result, model)
        data = json.loads(result.evidence[1].content)
        self.assertEqual(data["classification"], "timeout")
        self.assertEqual(data["classification_basis"], "test_deadline")
        self.assertEqual(data["cleanup_status"], "confirmed")
        self.assertLessEqual(data["cleanup_seconds"], 2.05)
        self.assertLess(time.monotonic() - started, 2.4)
        pgid = int(data["output"].strip())
        with self.assertRaises(ProcessLookupError):
            os.killpg(pgid, 0)
        self.assertIsNone(data["exit_code"])
        self.assertEqual(data["signal"], signal.SIGTERM)

    def test_parent_exit_and_pipe_eof_do_not_hide_live_descendants(self) -> None:
        child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready',flush=True); time.sleep(4)"
        self.permit(
            "import os, subprocess, sys\n"
            f"child = subprocess.Popen([sys.executable, '-c', {child!r}], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)\n"
            "child.stdout.readline()\nprint(os.getpgrp(), child.pid, flush=True)\n",
            timeout_seconds=1,
        )
        started = time.monotonic()
        result, _ = self.investigate()
        self.assertEqual(result.status, "completed", result.reason)
        data = json.loads(result.evidence[1].content)
        self.assertTrue(data["force_kill_attempted"])
        self.assertEqual(data["cleanup_status"], "confirmed")
        self.assertEqual(data["exit_code"], 0)
        pgid, child_pid = map(int, data["output"].split())
        with self.assertRaises(ProcessLookupError):
            os.killpg(pgid, 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)
        self.assertLess(time.monotonic() - started, 2.5)

    def test_exited_parent_does_not_wait_for_descendant_pipe_eof(self) -> None:
        self.permit(
            "import os, subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)'])\n"
            "print(os.getpgrp(), child.pid, flush=True)\n",
            timeout_seconds=0.5,
        )
        result, _ = self.investigate()
        self.assertEqual(result.status, "completed", result.reason)
        data = json.loads(result.evidence[1].content)
        self.assertEqual(data["classification"], "passed")
        self.assertEqual(data["cleanup_status"], "confirmed")
        pgid, child_pid = map(int, data["output"].split())
        with self.assertRaises(ProcessLookupError):
            os.killpg(pgid, 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_cleanup_failure_is_preserved_and_stops_new_operations(self) -> None:
        # 辅助父进程留在同一会话的另一进程组，短暂延迟回收已退出的组内子进程。
        # 僵尸不会响应 TERM/KILL，宿主必须如实报告组仍存在；夹具随后自行回收。
        self.permit(
            "import os, time\n"
            "pgid = os.getpgrp()\nr, w = os.pipe()\nsupervisor = os.fork()\n"
            "if supervisor == 0:\n"
            "    os.close(r)\n    os.setpgid(0, 0)\n    child = os.fork()\n"
            "    if child == 0:\n"
            "        os.setpgid(0, pgid)\n        os.write(w, str(os.getpid()).encode())\n        os._exit(0)\n"
            "    os.close(w)\n    time.sleep(3)\n    os.waitpid(child, 0)\n    os._exit(0)\n"
            "os.close(w)\nchild_id = os.read(r, 128).decode()\n"
            "print(pgid, supervisor, child_id, flush=True)\nos._exit(0)\n",
        )
        result: RunResult | None = None
        try:
            result, model = self.investigate()
            self.assertEqual(result.status, "system_failure", result.reason)
            self.assertEqual(result.reason, "cleanup_failed")
            self.assertEqual(len(model.requests), 2)
            data = json.loads(result.evidence[1].content)
            self.assertEqual(data["cleanup_status"], "cleanup_failed")
            self.assertEqual(data["exit_code"], 0)
            self.assertGreaterEqual(data["cleanup_seconds"], 1.9)
            self.assertLessEqual(data["cleanup_seconds"], 2.05)
            self.assertIn("cleanup_failed", result.markdown)
            try:
                os.killpg(int(data["output"].split()[0]), 0)
            except PermissionError:
                pass  # macOS 对仅余僵尸的组可能返回 EPERM，仍然不能确认组消失。
        finally:
            if result and len(result.evidence) > 1:
                pgid = int(json.loads(result.evidence[1].content)["output"].split()[0])
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        os.killpg(pgid, 0)
                    except ProcessLookupError:
                        break
                    except PermissionError:
                        pass
                    time.sleep(0.01)
                with self.assertRaises(ProcessLookupError):
                    os.killpg(pgid, 0)

    def test_timeout_force_kills_the_parent_and_its_descendant(self) -> None:
        child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready',flush=True); time.sleep(4)"
        self.permit(
            "import os, signal, subprocess, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"child = subprocess.Popen([sys.executable, '-c', {child!r}], stdout=subprocess.PIPE)\n"
            "child.stdout.readline()\nprint(os.getpgrp(), child.pid, flush=True)\ntime.sleep(4)\n",
            timeout_seconds=0.25,
        )
        result, model = self.investigate()
        self.assert_timeout_returned_to_model(result, model)
        data = json.loads(result.evidence[1].content)
        self.assertTrue(data["force_kill_attempted"])
        self.assertEqual(data["signal"], signal.SIGKILL)
        self.assertEqual(data["cleanup_status"], "confirmed")
        pgid, child_pid = map(int, data["output"].split())
        with self.assertRaises(ProcessLookupError):
            os.killpg(pgid, 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_startup_permission_mapping_is_frozen(self) -> None:
        self.permit("import sys\nprint(sys.argv[1])\n")
        self.config["permitted_tests"]["check"]["command"].append("ORIGINAL")

        def mutate_then_test(request: dict[str, Any]) -> dict[str, Any]:
            self.config["permitted_tests"]["check"]["command"][-1] = "CHANGED"
            self.config["permitted_tests"]["check"]["timeout_seconds"] = 0.000001
            return tool_call("run_test", {"test_id": "check"})

        model = ScriptedModel([
            tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2}),
            mutate_then_test, conclusion("E1", ("E2",)),
        ])
        result = run_analysis(self.config, transport=model)
        self.assertEqual(result.status, "completed", result.reason)
        self.assertEqual(json.loads(result.evidence[1].content)["output"], "ORIGINAL\n")
        self.assertEqual(result.config_snapshot["permitted_tests"]["check"]["command"][-1], "ORIGINAL")
        self.assertEqual(result.config_snapshot["permitted_tests"]["check"]["timeout_seconds"], 2)

    def test_invalid_permissions_are_rejected_before_model_calls(self) -> None:
        self.permit("print('unused')\n")
        program = str(Path(sys.executable).resolve())
        invalid_entries: list[dict[str, Any]] = [
            {"command": ["python3", "check.py"]},
            {"command": [str(self.root / "missing")]},
            {"command": [str(self.root)]},
            {"command": [str(self.root / "check.py")]},
            {"command": f"{program} check.py"},
            {"command": [program, 12]},
            {"command": [program, "bad\0arg"]},
            {"command": [program, "\ud800"]},
            {"timeout_seconds": True}, {"timeout_seconds": 0},
            {"timeout_seconds": float("nan")}, {"timeout_seconds": float("inf")},
            {"timeout_seconds": 10 ** 1000},
            {"assertion_failure_exit_codes": [0]}, {"assertion_failure_exit_codes": [True]},
            {"assertion_failure_exit_codes": [10, 10]},
            {"unknown": "value"},
        ]
        for entry in invalid_entries:
            with self.subTest(entry=entry):
                self.config["permitted_tests"] = {"check": {
                    "command": [program, "check.py"], "timeout_seconds": 1, **entry,
                }}
                model = ScriptedModel([])
                with self.assertRaises(ConfigError):
                    run_analysis(self.config, transport=model)
                self.assertEqual(model.requests, [])

    def test_python_virtual_environment_is_preserved(self) -> None:
        self.permit("import sys\nprint(sys.prefix)\n")
        result, _ = self.investigate()
        self.assertEqual(result.status, "completed", result.reason)
        self.assertEqual(json.loads(result.evidence[1].content)["output"].strip(), sys.prefix)
        self.assertEqual(result.config_snapshot["permitted_tests"]["check"]["command"][0], sys.executable)

    def test_invalid_json_does_not_start_a_test(self) -> None:
        self.permit("from pathlib import Path\nPath('started').touch()\n")
        call = tool_call("run_test", {})
        call["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{broken"
        model = ScriptedModel([
            call, tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2}),
            conclusion("E1"),
        ])
        result = run_analysis(self.config, transport=model)
        self.assertEqual(result.status, "completed", result.reason)
        feedback = json.loads(model.feedback()[0]["content"])
        self.assertEqual(feedback["error"], "invalid_arguments")
        self.assertFalse(feedback["started"])
        self.assertIsNone(feedback["exit_code"])
        self.assertFalse((self.root / "started").exists())

    def test_continuous_output_does_not_block_test_timeout(self) -> None:
        self.permit("import os,time\nend=time.monotonic()+4\nwhile time.monotonic()<end:\n    os.write(1,b'x'*8192)\n",
                    timeout_seconds=0.15)
        self.config["limits"]["max_test_output_bytes"] = 128
        result, model = self.investigate()
        self.assert_timeout_returned_to_model(result, model)
        data = json.loads(result.evidence[1].content)
        self.assertEqual(data["cleanup_status"], "confirmed")
        self.assertTrue(data["output_truncated"])
        self.assertEqual(data["output"], "x" * 128)
        self.assertLess(result.elapsed_seconds, 2.4)

    def test_total_deadline_stops_before_test_own_deadline(self) -> None:
        self.permit("import os,time\nprint(os.getpgrp(), flush=True)\ntime.sleep(4)\n",
                    timeout_seconds=0.5)
        self.config["limits"]["total_seconds"] = 0.3
        result, model = self.investigate()
        self.assertEqual((result.status, result.reason), ("timeout", "total_deadline"))
        self.assertEqual(len(model.requests), 2)
        self.assertEqual((result.model_calls, result.tool_calls), (2, 2))
        self.assertIsNone(result.report)
        data = json.loads(result.evidence[1].content)
        self.assertEqual(data["classification"], "timeout")
        self.assertEqual(data["classification_basis"], "total_deadline")
        self.assertEqual(data["cleanup_status"], "confirmed")
        self.assertGreaterEqual(result.elapsed_seconds, 0.3)
        self.assertIn('"classification": "timeout"', result.markdown)
        with self.assertRaises(ProcessLookupError):
            os.killpg(int(data["output"].strip()), 0)
