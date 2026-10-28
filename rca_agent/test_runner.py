import json
import os
import selectors
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import Limits, PermittedTest
from .control import RunControl
from .processes import cleanup_process
from .report import Evidence, evidence_feedback
from .tools import ToolError


def run_test(root: Path, arguments: Any, permitted: tuple[PermittedTest, ...],
             evidence_id: str, limits: Limits, control: RunControl) -> Evidence:
    if (not isinstance(arguments, dict) or set(arguments) != {"test_id"}
            or not isinstance(arguments["test_id"], str)):
        raise ToolError("invalid_arguments", "许可测试只接受 test_id，不接受命令或额外参数。")
    test = next((test for test in permitted if test.test_id == arguments["test_id"]), None)
    if test is None:
        raise ToolError("permission_denied", "测试编号不在本次启动许可映射中。")
    deadline = time.monotonic() + test.timeout_seconds
    control.checkpoint()
    try:
        process = subprocess.Popen(test.command, cwd=root, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True)
    except OSError as exc:
        data = {"test_id": test.test_id, "started": False, "output": "",
                "exit_code": None, "signal": None, "classification": "execution_error",
                "classification_basis": "start_failed", "start_errno": exc.errno,
                "cleanup_status": "not_started", "output_truncated": False}
        return Evidence(evidence_id, "test", test.test_id, json.dumps(data, ensure_ascii=False))
    output = bytearray()
    truncated = False
    stop_reason: str | None = None
    read_failed = False
    output_complete = False
    can_drain = False
    def retain(chunk: bytes) -> None:
        nonlocal truncated
        retained = chunk[:max(0, limits.max_test_output_bytes - len(output))]
        output.extend(retained)
        truncated |= len(retained) < len(chunk)

    assert process.stdout is not None
    stream = process.stdout
    try:
        os.set_blocking(process.stdout.fileno(), False)
        can_drain = True
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map() or process.poll() is None:
                if control.reason is not None:
                    stop_reason = control.reason
                    break
                parent_exited = process.poll() is not None
                remaining = deadline - time.monotonic()
                if remaining <= 0 and not parent_exited:
                    stop_reason = "test_deadline"
                    break
                for key, _ in selector.select(0 if parent_exited else max(0, min(0.02, remaining, control.deadline - time.monotonic()))):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    retain(chunk)
                if parent_exited:
                    break  # 后代可能持有管道，转入进程组清理，不能无限等待 EOF。
            output_complete = not selector.get_map()
    except OSError:
        read_failed = True
    except KeyboardInterrupt:
        control.cancellation.cancel()
        stop_reason = control.reason
    finally:
        def drain() -> None:
            nonlocal read_failed, output_complete
            if not can_drain:
                return  # 设置非阻塞失败时，不让清理进入阻塞管道读取。
            try:
                chunk = os.read(stream.fileno(), 65536)
            except BlockingIOError:
                return
            except OSError:
                read_failed = True
                return
            if not chunk:
                output_complete = True
            else:
                retain(chunk)

        try:
            cleanup = cleanup_process(process, control, "test:" + test.test_id, drain)
        finally:
            process.stdout.close()
    returncode = process.returncode
    if stop_reason is not None:
        classification = "cancelled" if stop_reason == "user_cancelled" else "timeout"
        basis = stop_reason
    elif read_failed:
        classification, basis = "execution_error", "output_read_failed"
    elif returncode == 0:
        classification, basis = "passed", "zero_exit"
    elif returncode in test.assertion_failure_exit_codes:
        classification, basis = "assertion_failed", "configured_exit_code"
    else:
        classification = "execution_error"
        basis = "signal" if returncode is not None and returncode < 0 else "unclassified_nonzero_exit"
    data = {"test_id": test.test_id, "started": True, "output": output.decode("utf-8", errors="replace"),
            "exit_code": returncode if returncode is not None and returncode >= 0 else None,
            "signal": -returncode if returncode is not None and returncode < 0 else None,
            "classification": classification, "classification_basis": basis,
            "cleanup_status": cleanup.status, "cleanup_seconds": cleanup.elapsed_seconds,
            "force_kill_attempted": cleanup.force_kill_attempted, "output_truncated": truncated}
    data["output_complete"] = output_complete
    return _snapshot(evidence_id, test.test_id, data, limits)


def _snapshot(evidence_id: str, test_id: str, data: dict[str, Any], limits: Limits) -> Evidence:
    def item() -> Evidence:
        return Evidence(evidence_id, "test", test_id, json.dumps(data, ensure_ascii=False))

    # 原始字节有界；再次按完整反馈预算裁剪，计入双层 JSON 转义和 UTF-8 替换字符。
    output = data["output"]
    if len(evidence_feedback(item()).encode("utf-8")) > limits.max_tool_feedback_bytes:
        data["output_truncated"] = True
    low, high = 0, len(output)
    while low < high:
        mid = (low + high + 1) // 2
        data["output"] = output[:mid]
        if (len(data["output"].encode("utf-8")) <= limits.max_test_output_bytes
                and len(evidence_feedback(item()).encode("utf-8")) <= limits.max_tool_feedback_bytes):
            low = mid
        else:
            high = mid - 1
    data["output"] = output[:low]
    data["output_truncated"] |= low < len(output)
    return item()
