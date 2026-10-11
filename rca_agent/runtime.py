import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import load_config
from .model import DeepSeekTransport, ModelError, ModelTransport, READ_TOOL, SYSTEM_PROMPT, parse_message
from .report import Evidence, ReportError, RunEvent, RunResult, RunStatus, render_markdown, validate_report
from .tools import ToolError, read_file


def run_analysis(
    config: Path | Mapping[str, Any], *, transport: ModelTransport | None = None,
    on_event: Callable[[RunEvent], None] | None = None,
) -> RunResult:
    """一次分析的公共入口；替身仅可接在外部模型通信边界。"""
    settings = load_config(config)
    started = time.monotonic()
    deadline = started + settings.limits.total_seconds
    client = transport if transport is not None else DeepSeekTransport()
    evidence = [Evidence(f"E{i}", "initial_material", f"initial_materials[{i - 1}]", text)
                for i, text in enumerate(settings.initial_materials, 1)]
    events: list[RunEvent] = []
    model_calls, tool_calls = 0, 0

    def emit(kind: str, detail: str) -> None:
        event = RunEvent(kind, detail)
        events.append(event)
        if on_event:
            on_event(event)

    def finish(status: RunStatus, reason: str, report: dict[str, Any] | None = None) -> RunResult:
        emit("finished", f"{status}: {reason}")
        result = RunResult(
            status=status, reason=reason, model=settings.model, evidence=evidence, events=events,
            model_calls=model_calls, tool_calls=tool_calls, elapsed_seconds=time.monotonic() - started,
            model_source="deepseek" if transport is None else "injected_transport",
            config_snapshot={"question": settings.question, "authorized_dir": str(settings.authorized_dir),
                             "model": settings.model, "limits": asdict(settings.limits)},
            report=report,
        )
        result.markdown = render_markdown(result)
        return result

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps({"question": settings.question,
            "initial_evidence": [asdict(item) for item in evidence]}, ensure_ascii=False)},
    ]
    emit("started", "授权与限制已固定。")
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return finish("timeout", "total_deadline")
            if model_calls >= settings.limits.max_model_calls or tool_calls >= settings.limits.max_tool_calls:
                return finish("limits_exhausted", "call_limit")
            model_calls += 1
            emit("model_attempt", f"第 {model_calls} 次模型请求。")
            response = client.complete({"model": settings.model, "messages": messages,
                "tools": [READ_TOOL], "response_format": {"type": "json_object"},
                "thinking": {"type": "disabled"}, "max_tokens": 4096, "stream": False}, remaining)
            if time.monotonic() >= deadline:
                return finish("timeout", "total_deadline")
            message = parse_message(response)
            messages.append(message)
            if not message.get("tool_calls"):
                report = validate_report(message["content"], evidence)
                return finish("completed", "valid_report", report)
            for call in message["tool_calls"]:
                if time.monotonic() >= deadline:
                    return finish("timeout", "total_deadline")
                if tool_calls >= settings.limits.max_tool_calls or model_calls >= settings.limits.max_model_calls:
                    return finish("limits_exhausted", "call_limit")
                tool_calls += 1
                emit("tool_attempt", f"第 {tool_calls} 次工具请求。")
                feedback: dict[str, Any]
                try:
                    function = call["function"]
                    if function["name"] != "read_file":
                        raise ToolError("unknown_tool", "本版本只支持 read_file。")
                    try:
                        arguments = json.loads(function["arguments"])
                    except ValueError as exc:
                        raise ToolError("invalid_arguments", "工具参数必须是 JSON 对象。") from exc
                    data = read_file(settings.authorized_dir, arguments, settings.limits.max_read_bytes)
                    item = Evidence(f"E{len(evidence) + 1}", "file", **data)
                    evidence.append(item)
                    feedback = {"ok": True, "evidence": asdict(item)}
                    emit("evidence_saved", item.id)
                except ToolError as exc:
                    feedback = {"ok": False, "error": exc.code, "message": str(exc)}
                    emit("tool_rejected", exc.code)
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": json.dumps(feedback, ensure_ascii=False)})
    except ModelError as exc:
        return finish("timeout" if time.monotonic() >= deadline else "system_failure", str(exc))
    except ReportError as exc:
        return finish("system_failure", "invalid_report: " + str(exc))
    except KeyboardInterrupt:
        return finish("user_cancelled", "keyboard_interrupt")
    except Exception:
        # 外部通信异常可能包含凭据，终止记录只保留分类，不回显异常文本。
        return finish("system_failure", "unexpected_error")
