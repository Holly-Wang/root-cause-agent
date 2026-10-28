import json
import http.client
import os
import socket
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from rca_agent import CancellationToken, RunControl, run_analysis
from rca_agent.model import DeepSeekTransport, ModelError
from tests.test_discovery import ScriptedModel, tool_call, conclusion


class WaitingModel:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.ready = threading.Event()
        self.closed = threading.Event()

    def complete(self, request: dict[str, Any], control: RunControl) -> dict[str, Any]:
        self.requests.append(json.loads(json.dumps(request)))
        if len(self.requests) == 1:
            return tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2})
        self.ready.set()
        try:
            control.wait()
            return conclusion("E2")
        finally:
            self.closed.set()


class LocalModelServer:
    """只替代外部模型服务；接受真实 TCP 请求，等待客户端关闭。"""

    def __init__(self, cancellation: CancellationToken | None = None, *, dribble: bool = False,
                 responses: list[dict[str, Any]] | None = None) -> None:
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(3)
        self.port = self.listener.getsockname()[1]
        self.cancellation = cancellation
        self.dribble = dribble
        self.responses = responses
        self.ready = threading.Event()
        self.closed = threading.Event()
        self.errors: list[str] = []
        self.bodies: list[bytes] = []
        self.thread = threading.Thread(target=self.serve)

    def serve(self) -> None:
        try:
            responses: list[dict[str, Any] | None] = list(self.responses) if self.responses else [None]
            for response in responses:
                with self.listener.accept()[0] as connection:
                    connection.settimeout(2)
                    request = b""
                    while b"\r\n\r\n" not in request:
                        chunk = connection.recv(65536)
                        if not chunk:
                            return
                        request += chunk
                    headers, body = request.split(b"\r\n\r\n", 1)
                    length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                                  if line.lower().startswith(b"content-length:"))
                    while len(body) < length:
                        chunk = connection.recv(65536)
                        if not chunk:
                            return
                        body += chunk
                    self.bodies.append(body[:length])
                    self.ready.set()
                    if response is not None:
                        payload = json.dumps(response).encode()
                        connection.sendall(f"HTTP/1.1 200 OK\r\nContent-Length: {len(payload)}\r\n\r\n".encode() + payload)
                        continue
                    if self.cancellation:
                        self.cancellation.cancel()
                    if self.dribble:
                        connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1048576\r\n\r\n")
                        connection.settimeout(0.03)
                    end = time.monotonic() + 4
                    while time.monotonic() < end:
                        try:
                            if not connection.recv(65536):
                                self.closed.set()
                                return
                        except socket.timeout:
                            if not self.dribble:
                                raise
                            connection.sendall(b" ")
                    self.errors.append("client_did_not_close")
            self.closed.set()
        except (BrokenPipeError, ConnectionResetError):
            self.closed.set()
        except (OSError, TimeoutError) as exc:
            self.errors.append(type(exc).__name__)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.listener.close()
        self.thread.join(4)
        if self.thread.is_alive():
            raise AssertionError("本机验收服务未收尾。")


class RunControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "material"
        self.root.mkdir()
        (self.root / "source.py").write_text("value = 1\nvalue += 1\n", encoding="utf-8")
        self.config: dict[str, Any] = {
            "question": "受控停止", "initial_materials": ["用户提供的现象"],
            "authorized_dir": str(self.root), "model": "deepseek-flash",
            "limits": {"total_seconds": 5},
        }

    def network_environment(self, server: LocalModelServer) -> dict[str, str]:
        bootstrap = Path(self.temp.name) / "network-fixture"
        bootstrap.mkdir(exist_ok=True)
        (bootstrap / "sitecustomize.py").write_text(
            "import http.client, os\n"
            "class LocalConnection(http.client.HTTPConnection):\n"
            "    def __init__(self, host, timeout=None):\n"
            "        super().__init__('127.0.0.1', int(os.environ['RCA_LOCAL_PORT']), timeout=timeout)\n"
            "http.client.HTTPSConnection = LocalConnection\n", encoding="utf-8")
        return {"DEEPSEEK_API_KEY": "local-test-placeholder", "PYTHONPATH": str(bootstrap),
                "RCA_LOCAL_PORT": str(server.port)}

    def wait_for_file(self, path: Path, process: subprocess.Popen[str]) -> None:
        end = time.monotonic() + 4
        while not path.exists() and process.poll() is None and time.monotonic() < end:
            threading.Event().wait(0.01)
        self.assertTrue(path.exists(), "未观察到夹具的明确就绪事实。")

    def start_cli(self, bootstrap_code: str) -> tuple[subprocess.Popen[str], Path]:
        bootstrap = Path(self.temp.name) / "cli-fixture"
        bootstrap.mkdir()
        (bootstrap / "sitecustomize.py").write_text(bootstrap_code, encoding="utf-8")
        config = Path(self.temp.name) / "config.json"
        config.write_text(json.dumps(self.config), encoding="utf-8")
        output = Path(self.temp.name) / "cli-output"
        env = {**os.environ, "PYTHONPATH": str(bootstrap) + os.pathsep + str(Path(__file__).resolve().parents[1])}
        process = subprocess.Popen([sys.executable, "-m", "rca_agent", "--config", str(config),
                                    "--output", str(output)], env=env, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)

        def reap() -> None:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=3)

        self.addCleanup(reap)
        return process, output

    def permit_active_test(self) -> Path:
        child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready',flush=True); time.sleep(8)"
        ready = self.root / "ready.json"
        (self.root / "check.py").write_text(
            "import json,os,signal,subprocess,sys,time\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"child = subprocess.Popen([sys.executable, '-c', {child!r}], stdout=subprocess.PIPE)\n"
            "child.stdout.readline()\n"
            "print('READY', os.getpgrp(), child.pid, flush=True)\n"
            "Path('ready.tmp').write_text(json.dumps([os.getpgrp(), child.pid]))\n"
            "Path('ready.tmp').replace('ready.json')\ntime.sleep(8)\n", encoding="utf-8")
        self.config["permitted_tests"] = {"active": {
            "command": [sys.executable, "check.py"], "timeout_seconds": 3,
        }}
        self.config["limits"]["max_test_output_bytes"] = 128
        return ready

    def active_model(self) -> ScriptedModel:
        return ScriptedModel([
            tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2}),
            tool_call("run_test", {"test_id": "active"}, "active-call"), conclusion("E2"),
        ])

    def assert_stop_survives_cleanup_failure(self, *, cancel: bool) -> None:
        self.permit_active_test()
        (self.root / "check.py").write_text(
            "import json,os,signal,time\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "pgid=os.getpgrp()\nr,w=os.pipe()\nsupervisor=os.fork()\n"
            "if supervisor == 0:\n"
            "    os.close(r)\n    os.setpgid(0,0)\n    child=os.fork()\n"
            "    if child == 0:\n"
            "        os.setpgid(0,pgid)\n        os.write(w,str(os.getpid()).encode())\n        os._exit(0)\n"
            "    os.close(w)\n    end=time.monotonic()+6\n"
            "    while not Path('reap').exists() and time.monotonic()<end:\n        time.sleep(0.01)\n"
            "    os.waitpid(child,0)\n    os._exit(0)\n"
            "os.close(w)\nchild=int(os.read(r,128))\n"
            "print('READY',pgid,child,flush=True)\n"
            "Path('ready.tmp').write_text(json.dumps([pgid,child]))\n"
            "Path('ready.tmp').replace('ready.json')\ntime.sleep(8)\n", encoding="utf-8")
        cancellation = CancellationToken()
        ready = self.root / "ready.json"
        self.config["limits"]["total_seconds"] = 0.5

        def cancel_when_ready() -> None:
            end = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < end:
                threading.Event().wait(0.01)
            if cancel and ready.exists():
                cancellation.cancel()

        controller = threading.Thread(target=cancel_when_ready)
        controller.start()
        model = self.active_model()
        try:
            result = run_analysis(self.config, transport=model, cancellation=cancellation)
            expected = ("user_cancelled", "user_cancelled") if cancel else ("timeout", "total_deadline")
            self.assertEqual((result.status, result.reason), expected)
            self.assertEqual(len(model.requests), 2)
            self.assertEqual(result.cleanup_status, "cleanup_failed")
            self.assertGreaterEqual(result.cleanup_seconds, 1.9)
            self.assertLessEqual(result.cleanup_seconds, 2.1)
            self.assertLess(result.elapsed_seconds, 2.9)
            self.assertEqual(result.evidence[1].kind, "file")
            data = json.loads(result.evidence[2].content)
            self.assertEqual(data["classification_basis"], expected[1])
            self.assertEqual(data["cleanup_status"], "cleanup_failed")
            self.assertIn("READY", data["output"])
            self.assertTrue(any(e.kind == "cleanup_result" and "cleanup_failed" in e.detail for e in result.events))
            output = Path(self.temp.name) / "failed-cleanup"
            result.write_to(output)
            saved = json.loads((output / "run.json").read_text())
            self.assertEqual(saved["reason"], expected[1])
            self.assertEqual(saved["cleanup"][0]["status"], "cleanup_failed")
            self.assertIn("cleanup_failed", result.markdown)
        finally:
            controller.join(4)
            (self.root / "reap").touch()
            if ready.exists():
                pgid = json.loads(ready.read_text())[0]
                end = time.monotonic() + 2
                while time.monotonic() < end:
                    try:
                        os.killpg(pgid, 0)
                    except ProcessLookupError:
                        break
                    except PermissionError:
                        pass
                    threading.Event().wait(0.01)
                with self.assertRaises(ProcessLookupError):
                    os.killpg(pgid, 0)

    def test_total_deadline_survives_cleanup_failure(self) -> None:
        self.assert_stop_survives_cleanup_failure(cancel=False)

    def test_user_cancellation_survives_cleanup_failure(self) -> None:
        self.assert_stop_survives_cleanup_failure(cancel=True)

    def test_cancellation_before_start_does_not_dispatch(self) -> None:
        cancellation = CancellationToken()
        cancellation.cancel()
        model = ScriptedModel([])
        result = run_analysis(self.config, transport=model, cancellation=cancellation)
        self.assertEqual((result.status, result.reason), ("user_cancelled", "user_cancelled"))
        self.assertEqual((result.model_calls, result.tool_calls), (0, 0))
        self.assertEqual(model.requests, [])
        self.assertEqual(result.evidence[0].content, "用户提供的现象")
        output = Path(self.temp.name) / "saved"
        result.write_to(output)
        self.assertEqual(json.loads((output / "run.json").read_text())["status"], "user_cancelled")

    def test_cancel_at_model_dispatch_does_not_call_transport(self) -> None:
        cancellation = CancellationToken()
        model = WaitingModel()
        result = run_analysis(self.config, transport=model, cancellation=cancellation,
                              on_event=lambda event: cancellation.cancel() if event.kind == "model_attempt" else None)
        self.assertEqual(result.status, "user_cancelled")
        self.assertEqual(model.requests, [])
        self.assertEqual(result.tool_calls, 0)

    def test_cancel_at_tool_dispatch_does_not_read_more_evidence(self) -> None:
        cancellation = CancellationToken()
        model = self.active_model()
        result = run_analysis(self.config, transport=model, cancellation=cancellation,
                              on_event=lambda event: cancellation.cancel() if event.kind == "tool_attempt" else None)
        self.assertEqual(result.status, "user_cancelled")
        self.assertEqual(len(model.requests), 1)
        self.assertEqual([item.kind for item in result.evidence], ["initial_material"])

    def test_total_deadline_interrupts_model_wait_after_closing_communication(self) -> None:
        self.config["limits"]["total_seconds"] = 0.2
        model = WaitingModel()
        closed_at_finish: list[bool] = []
        started = time.monotonic()
        result = run_analysis(self.config, transport=model,
                              on_event=lambda event: closed_at_finish.append(model.closed.is_set())
                              if event.kind == "finished" else None)
        self.assertEqual((result.status, result.reason), ("timeout", "total_deadline"))
        self.assertTrue(model.ready.is_set())
        self.assertEqual(closed_at_finish, [True])
        self.assertEqual((result.model_calls, result.tool_calls), (2, 1))
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(result.evidence[1].content, "value = 1\nvalue += 1\n")
        self.assertIsNone(result.report)
        self.assertLess(time.monotonic() - started, 1)

    def test_cancellation_interrupts_model_wait(self) -> None:
        cancellation = CancellationToken()
        model = WaitingModel()

        def cancel_when_ready() -> None:
            if model.ready.wait(2):
                cancellation.cancel()

        controller = threading.Thread(target=cancel_when_ready)
        controller.start()
        try:
            result = run_analysis(self.config, transport=model, cancellation=cancellation)
        finally:
            controller.join(3)
        self.assertFalse(controller.is_alive())
        self.assertTrue(model.ready.is_set())
        self.assertTrue(model.closed.is_set())
        self.assertEqual((result.status, result.reason), ("user_cancelled", "user_cancelled"))
        self.assertEqual((result.model_calls, result.tool_calls), (2, 1))
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(result.evidence[1].kind, "file")

    def test_real_transport_closes_waiting_request_on_cancel(self) -> None:
        cancellation = CancellationToken()
        server = LocalModelServer(cancellation)
        self.addCleanup(server.close)
        server.start()
        started = time.monotonic()
        with patch.dict(os.environ, self.network_environment(server)):
            result = run_analysis(self.config, transport=DeepSeekTransport(), cancellation=cancellation)
        self.assertEqual((result.status, result.reason), ("user_cancelled", "user_cancelled"))
        self.assertTrue(server.ready.is_set())
        self.assertTrue(server.closed.wait(1))
        self.assertEqual(server.errors, [])
        self.assertEqual(result.model_calls, 1)
        self.assertEqual(result.cleanup_status, "confirmed")
        self.assertLess(time.monotonic() - started, 1.5)

    def test_real_transport_total_deadline_bounds_a_trickling_response(self) -> None:
        server = LocalModelServer(dribble=True)
        self.addCleanup(server.close)
        self.config["limits"]["total_seconds"] = 0.4
        server.start()
        started = time.monotonic()
        with patch.dict(os.environ, self.network_environment(server)):
            result = run_analysis(self.config, transport=DeepSeekTransport())
        self.assertTrue(server.ready.is_set())
        self.assertTrue(server.closed.wait(1))
        self.assertEqual(server.errors, [])
        self.assertEqual((result.status, result.reason), ("timeout", "total_deadline"))
        self.assertEqual((result.model_calls, result.tool_calls), (1, 0))
        self.assertEqual(result.cleanup_status, "confirmed")
        self.assertLess(time.monotonic() - started, 1.3)
        with self.assertRaises(ProcessLookupError):
            os.killpg(result.cleanup[0].process_group_id, 0)

    def test_real_transport_success_is_consumed_after_worker_exit(self) -> None:
        server = LocalModelServer(responses=[
            tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2}),
            conclusion("E2"),
        ])
        self.addCleanup(server.close)
        server.start()
        with patch.dict(os.environ, self.network_environment(server)):
            result = run_analysis(self.config, transport=DeepSeekTransport())
        self.assertEqual(result.status, "completed", result.reason)
        self.assertTrue(server.closed.wait(1))
        self.assertEqual(server.errors, [])
        self.assertEqual(len(result.cleanup), 2)
        self.assertEqual(result.cleanup_status, "confirmed")
        self.assertEqual(result.resource_usage["largest_context_bytes"], max(len(body) for body in server.bodies))
        for body in server.bodies:
            request = json.loads(body)
            self.assertEqual(body, json.dumps(request, ensure_ascii=False, allow_nan=False,
                                             sort_keys=True, separators=(",", ":")).encode("utf-8"))
            self.assertEqual(len(request["tools"]), 5)
        for cleanup in result.cleanup:
            with self.assertRaises(ProcessLookupError):
                os.killpg(cleanup.process_group_id, 0)

    def test_total_deadline_interrupts_active_test_and_preserves_partial_evidence(self) -> None:
        ready = self.permit_active_test()
        self.config["limits"]["total_seconds"] = 0.5
        model = self.active_model()
        result = run_analysis(self.config, transport=model)
        self.assertEqual((result.status, result.reason), ("timeout", "total_deadline"))
        self.assertTrue(ready.exists(), "必须实际进入受控测试，不能只在派发前到限。")
        self.assertEqual((result.model_calls, result.tool_calls), (2, 2))
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(result.evidence[1].kind, "file")
        data = json.loads(result.evidence[2].content)
        self.assertEqual(data["classification_basis"], "total_deadline")
        self.assertEqual(data["cleanup_status"], "confirmed")
        self.assertEqual(data["signal"], signal.SIGKILL)
        self.assertIn("READY", data["output"])
        self.assertLessEqual(len(data["output"].encode()), 128)
        self.assertLess(result.elapsed_seconds, 2)
        self.assertEqual(result.cleanup_status, "confirmed")
        self.assertLessEqual(result.cleanup_seconds, 2.05)
        for pid in json.loads(ready.read_text()):
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_cancellation_interrupts_active_test_after_ready(self) -> None:
        ready = self.permit_active_test()
        cancellation = CancellationToken()

        def cancel_when_ready() -> None:
            limit = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < limit:
                threading.Event().wait(0.01)
            if ready.exists():
                cancellation.cancel()

        controller = threading.Thread(target=cancel_when_ready)
        model = self.active_model()
        controller.start()
        try:
            result = run_analysis(self.config, transport=model, cancellation=cancellation)
        finally:
            controller.join(4)
        self.assertFalse(controller.is_alive())
        self.assertTrue(ready.exists())
        self.assertEqual((result.status, result.reason), ("user_cancelled", "user_cancelled"))
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(result.evidence[1].kind, "file")
        data = json.loads(result.evidence[2].content)
        self.assertEqual(data["classification"], "cancelled")
        self.assertEqual(data["classification_basis"], "user_cancelled")
        self.assertEqual(data["cleanup_status"], "confirmed")
        self.assertIn("READY", data["output"])
        self.assertLess(result.elapsed_seconds, 2)
        for pid in json.loads(ready.read_text()):
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        output = Path(self.temp.name) / "cancelled"
        result.write_to(output)
        saved = json.loads((output / "run.json").read_text())
        self.assertEqual(saved["reason"], "user_cancelled")
        self.assertEqual(saved["cleanup_status"], "confirmed")

    def test_stop_wins_over_late_results_and_retryable_errors(self) -> None:
        responses: list[dict[str, Any] | ModelError] = [
            conclusion("E2"), {"choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": "invalid report"}}]}, ModelError("request_timeout", retryable=True)]
        for response in responses:
            with self.subTest(response_type=type(response).__name__):
                cancellation = CancellationToken()

                class LateModel(WaitingModel):
                    def complete(self, request: dict[str, Any], control: RunControl) -> dict[str, Any]:
                        self.requests.append(json.loads(json.dumps(request)))
                        if len(self.requests) == 1:
                            return tool_call("read_file", {"path": "source.py", "start_line": 1, "end_line": 2})
                        cancellation.cancel()
                        if isinstance(response, ModelError):
                            raise response
                        return response

                model = LateModel()
                result = run_analysis(self.config, transport=model, cancellation=cancellation)
                self.assertEqual((result.status, result.reason), ("user_cancelled", "user_cancelled"))
                self.assertEqual((result.model_calls, result.tool_calls), (2, 1))
                self.assertEqual(len(model.requests), 2)
                self.assertIsNone(result.report)
                self.assertFalse(any(e.kind == "report_validation_feedback" for e in result.events))

    def test_model_error_details_are_not_saved(self) -> None:
        class FailingModel:
            def complete(self, request: dict[str, Any], control: RunControl) -> dict[str, Any]:
                raise ModelError("external-exception-secret-placeholder")

        result = run_analysis(self.config, transport=FailingModel())
        output = Path(self.temp.name) / "safe-error"
        result.write_to(output)
        self.assertEqual(result.status, "system_failure")
        self.assertNotIn("external-exception-secret-placeholder", (output / "run.json").read_text())
        self.assertNotIn("external-exception-secret-placeholder", result.markdown)

    def test_cli_sigint_before_dispatch_saves_cancelled_run(self) -> None:
        ready = Path(self.temp.name) / "model-construction-ready"
        process, output = self.start_cli(
            "import signal,threading\nfrom pathlib import Path\nimport rca_agent.runtime as runtime\n"
            "class StartupModel:\n"
            "    def __init__(self):\n"
            "        pending=threading.Event()\n        previous=signal.getsignal(signal.SIGINT)\n"
            "        def forward(signum,frame):\n            pending.set()\n"
            "            if callable(previous):\n                previous(signum,frame)\n"
            "        signal.signal(signal.SIGINT,forward)\n"
            f"        Path({str(ready)!r}).touch()\n"
            "        try:\n            pending.wait(2)\n"
            "        finally:\n            signal.signal(signal.SIGINT,previous)\n"
            "    def complete(self, request, control):\n        raise RuntimeError('must not dispatch')\n"
            "runtime.DeepSeekTransport = StartupModel\n")
        self.wait_for_file(ready, process)
        process.send_signal(signal.SIGINT)
        _, stderr = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 1, stderr)
        saved = json.loads((output / "run.json").read_text())
        self.assertEqual((saved["status"], saved["reason"]), ("user_cancelled", "user_cancelled"))
        self.assertEqual(saved["model_calls"], 0)
        self.assertEqual(saved["evidence"][0]["content"], "用户提供的现象")

    def test_cli_repeated_sigint_waits_for_active_test_cleanup(self) -> None:
        ready = self.permit_active_test()
        script = self.root / "check.py"
        script.write_text(script.read_text().replace(
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n",
            "signal.signal(signal.SIGTERM, lambda *args: Path('term_seen').touch())\n", 1), encoding="utf-8")
        process, output = self.start_cli(
            "import rca_agent.runtime as runtime\nfrom tests.test_discovery import ScriptedModel,tool_call,conclusion\n"
            "runtime.DeepSeekTransport=lambda: ScriptedModel([\n"
            "    tool_call('read_file',{'path':'source.py','start_line':1,'end_line':2}),\n"
            "    tool_call('run_test',{'test_id':'active'}),conclusion('E2')])\n")
        self.wait_for_file(ready, process)
        started = time.monotonic()
        process.send_signal(signal.SIGINT)
        self.wait_for_file(self.root / "term_seen", process)
        process.send_signal(signal.SIGINT)
        _, stderr = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 1, stderr)
        saved = json.loads((output / "run.json").read_text())
        self.assertEqual((saved["status"], saved["reason"]), ("user_cancelled", "user_cancelled"))
        self.assertEqual((saved["model_calls"], saved["tool_calls"]), (2, 2))
        self.assertEqual(saved["cleanup_status"], "confirmed")
        self.assertLessEqual(saved["cleanup_seconds"], 2.05)
        self.assertLess(time.monotonic() - started, 2.3)
        data = json.loads(saved["evidence"][2]["content"])
        self.assertEqual(data["classification"], "cancelled")
        self.assertIn("READY", data["output"])
        self.assertTrue((output / "report.md").exists())
        self.assertFalse((self.root / "run.json").exists())
        for pid in json.loads(ready.read_text()):
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
