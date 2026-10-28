"""人类答复的协作式输入 interface；分析循环持有会话、权限和预算。"""

import json
import os
import select
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Protocol

from .control import RunControl
from .config import Limits
from .report import Evidence
from .tools import ToolError, read_file
from .serialization import json_bytes


@dataclass(frozen=True)
class SupplementRequest:
    gap: str
    question: str
    max_bytes: int
    error: str | None = None


class SupplementLimit(Exception):
    def __init__(self, resource: str, actual: int, limit: int, unit: str = "bytes", *, at_least: bool = False) -> None:
        self.resource = resource
        self.detail: dict[str, Any] = {"resource": resource, "actual_" + unit: actual, "limit_" + unit: limit}
        if at_least:
            self.detail["size_is_lower_bound"] = True
        super().__init__(resource)


class InputProvider(Protocol):
    """receive 返回答复 JSON 对象或 None（EOF）；request.error 是上一答复的拒绝码。

    必须响应同一 control，停止后先收尾自有等待再传播 RunStopped；不得遗留输入线程、
    重置期限、私自调用模型或改写授权/预算。宿主不以遗弃线程包装任意阻塞 adapter。
    """

    def receive(self, request: SupplementRequest, control: RunControl) -> Any: ...


class StdinInputProvider:
    """macOS/Linux 的有界逐行 stdin adapter；不启动线程，不接管 stdin 所有权。"""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._eof = False

    def receive(self, request: SupplementRequest, control: RunControl) -> Any:
        while True:
            control.checkpoint()
            newline = self._buffer.find(b"\n")
            if newline >= 0 or self._eof:
                length = newline if newline >= 0 else len(self._buffer)
                line = bytes(self._buffer[:length])
                del self._buffer[:length + (1 if newline >= 0 else 0)]
                if self._eof and not line and newline < 0:
                    return None
                if len(line) > request.max_bytes:
                    raise SupplementLimit("max_supplement_bytes", len(line), request.max_bytes)
                try:
                    text = line.decode("utf-8").rstrip("\r")
                    if text == "/cancel":
                        return {"kind": "cancel"}
                    if text == "/decline":
                        return {"kind": "decline"}
                    if text.lstrip().startswith(("{", "[")):
                        return json.loads(text)
                    return {"kind": "text", "text": text}
                except (UnicodeError, ValueError) as exc:
                    raise ToolError("invalid_input_encoding", "输入必须为 UTF-8 文本或单行 JSON。") from exc
            if len(self._buffer) > request.max_bytes:
                raise SupplementLimit("max_supplement_bytes", len(self._buffer), request.max_bytes, at_least=True)
            fd = sys.stdin.fileno()
            if select.select([fd], [], [], min(0.02, control.remaining))[0]:
                chunk = os.read(fd, min(4096, request.max_bytes + 1 - len(self._buffer)))
                if not chunk:
                    self._eof = True
                self._buffer.extend(chunk)


def request_supplement(arguments: Any, provider: InputProvider | None, control: RunControl,
                       evidence_id: str, emit: Callable[[str, str], None], root: Path,
                       limits: Limits) -> Evidence | dict[str, Any]:
    if (not isinstance(arguments, dict) or set(arguments) != {"gap", "question"}
            or not all(isinstance(x, str) and x.strip() for x in arguments.values())):
        raise ToolError("invalid_arguments", "提问需要非空 gap 和 question。")
    request = SupplementRequest(arguments["gap"], arguments["question"], limits.max_supplement_bytes)
    for _ in range(limits.max_supplement_attempts):
        prompt = json.dumps(asdict(request), ensure_ascii=False)
        prompt_bytes = len(prompt.encode("utf-8"))
        if prompt_bytes > limits.max_tool_feedback_bytes:
            raise SupplementLimit("max_tool_feedback_bytes", prompt_bytes, limits.max_tool_feedback_bytes)
        emit("waiting_for_user", prompt)
        control.checkpoint()
        try:
            answer = provider.receive(request, control) if provider is not None else None
            control.checkpoint()
            if answer == {"kind": "cancel"}:
                control.cancellation.cancel()
                control.checkpoint()
            if answer is None or answer == {"kind": "decline"}:
                return {"ok": True, "declined": True,
                        "reason": "input_unavailable" if provider is None else "eof" if answer is None else "user_declined",
                        "message": "未取得补充；请保留缺口并诚实总结，不能声称已经验证。"}
            try:
                size = len(json_bytes(answer))
            except (TypeError, ValueError, UnicodeError) as exc:
                raise ToolError("invalid_supplement", "答复必须是可序列化的 UTF-8 JSON。") from exc
            if size > limits.max_supplement_bytes:
                raise SupplementLimit("max_supplement_bytes", size, limits.max_supplement_bytes)
            if isinstance(answer, dict) and answer.get("kind") == "file":
                return Evidence(evidence_id, "file", **read_file(
                    root, {key: value for key, value in answer.items() if key != "kind"}, limits.max_read_bytes))
            if (not isinstance(answer, dict) or set(answer) != {"kind", "text"}
                    or answer["kind"] != "text" or not isinstance(answer["text"], str) or not answer["text"].strip()):
                raise ToolError("invalid_supplement", "补充需要非空文本或授权范围内文件引用；扩大授权需修改配置重新启动。")
            return Evidence(evidence_id, "user_supplement", "user_reply", answer["text"])
        except ToolError as exc:
            if exc.code in ("read_limit", "scan_limit"):
                resource = "max_read_bytes" if exc.code == "read_limit" else "file_scan_bytes"
                limit = limits.max_read_bytes if exc.code == "read_limit" else 262144
                raise SupplementLimit(resource, limit + 1, limit, at_least=True) from exc
            emit("input_rejected", json.dumps({"error": exc.code,
                 "message": "补充被拒绝；需要扩大目录或测试许可时请修改配置重新启动。"}, ensure_ascii=False))
            request = replace(request, error=exc.code)
    raise SupplementLimit("max_supplement_attempts", limits.max_supplement_attempts,
                          limits.max_supplement_attempts, "attempts")
