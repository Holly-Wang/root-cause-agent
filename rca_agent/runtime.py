import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import load_config
from .control import CancellationToken, RunControl, RunStopped
from .model import (
    DeepSeekTransport,
    ModelError,
    ModelTransport,
    READ_TOOL,
    LIST_DIRECTORY_TOOL,
    SEARCH_TEXT_TOOL,
    RUN_TEST_TOOL,
    ASK_USER_TOOL,
    SYSTEM_PROMPT,
    parse_message,
)
from .report import (
    Evidence,
    evidence_feedback,
    ReportError,
    RunEvent,
    RunResult,
    RunStatus,
    render_markdown,
    validate_report,
)
from .tools import ToolError, list_directory, read_file, search_text
from .test_runner import run_test
from .serialization import json_bytes
from .supplement import InputProvider, SupplementLimit, request_supplement


def run_analysis(
    config: Path | Mapping[str, Any],
    *,
    transport: ModelTransport | None = None,
    on_event: Callable[[RunEvent], None] | None = None,
    cancellation: CancellationToken | None = None,
    input_provider: InputProvider | None = None,
) -> RunResult:
    """公共分析入口；外部模型通信和人类答复可注入协作式 adapter。"""
    settings = load_config(config)
    started = time.monotonic()
    deadline = started + settings.limits.total_seconds
    control = RunControl(deadline, cancellation if cancellation is not None else CancellationToken())
    client = transport if transport is not None else DeepSeekTransport()
    evidence: list[Evidence] = []
    evidence_bytes = 0
    largest_context_bytes = 0
    events: list[RunEvent] = []
    model_calls, tool_calls = 0, 0
    report_repairs_used = 0
    model_retries_used = 0

    def emit(kind: str, detail: str) -> None:
        event = RunEvent(kind, detail)
        events.append(event)
        if on_event:
            on_event(event)

    def finish(
        status: RunStatus, reason: str, report: dict[str, Any] | None = None
    ) -> RunResult:
        for cleanup in control.cleanup:
            emit("cleanup_result", json.dumps(asdict(cleanup), ensure_ascii=False))
        if control.reason is not None:
            reason = control.reason
            status = "timeout" if reason == "total_deadline" else "user_cancelled"
            report = None
        emit("finished", f"{status}: {reason}")
        result = RunResult(
            status=status,
            reason=reason,
            model=settings.model,
            evidence=evidence,
            events=events,
            model_calls=model_calls,
            tool_calls=tool_calls,
            elapsed_seconds=time.monotonic() - started,
            model_source="deepseek" if transport is None else "injected_transport",
            cleanup=list(control.cleanup),
            cleanup_status=("cleanup_failed" if any(item.status == "cleanup_failed" for item in control.cleanup)
                            else "confirmed" if control.cleanup else "not_needed"),
            cleanup_seconds=sum(item.elapsed_seconds for item in control.cleanup),
            resource_usage={"evidence_bytes": evidence_bytes, "largest_context_bytes": largest_context_bytes},
            config_snapshot={
                "question": settings.question,
                "authorized_dir": str(settings.authorized_dir),
                "model": settings.model,
                "limits": asdict(settings.limits),
                "permitted_tests": {test.test_id: {
                    "command": list(test.command), "timeout_seconds": test.timeout_seconds,
                    "assertion_failure_exit_codes": list(test.assertion_failure_exit_codes),
                } for test in settings.permitted_tests},
            },
            report=report,
        )
        result.markdown = render_markdown(result)
        return result

    def context_fits(request: dict[str, Any]) -> bool:
        nonlocal largest_context_bytes
        actual_bytes = len(json_bytes(request))
        largest_context_bytes = max(largest_context_bytes, actual_bytes)
        if actual_bytes <= settings.limits.max_context_bytes:
            return True
        emit("resource_limit", json.dumps({"resource": "max_context_bytes",
                                          "actual_bytes": actual_bytes,
                                          "limit_bytes": settings.limits.max_context_bytes}))
        return False

    def admit(item: Evidence, tool_call_id: str | None = None) -> bool:
        nonlocal evidence_bytes
        candidate_bytes = len(json_bytes(asdict(item)))
        actual_bytes = evidence_bytes + candidate_bytes
        if actual_bytes > settings.limits.max_evidence_bytes:
            emit("resource_limit", json.dumps({
                "resource": "max_evidence_bytes", "limit_bytes": settings.limits.max_evidence_bytes,
                "actual_bytes": actual_bytes, "candidate_bytes": candidate_bytes,
                "retained_bytes": evidence_bytes, "kind": item.kind, "evidence_id": item.id,
                "source": item.source[:256], "source_truncated": len(item.source) > 256,
                "tool_call_id": tool_call_id,
            }, ensure_ascii=False))
            return False
        evidence.append(item)
        evidence_bytes = actual_bytes
        emit("evidence_saved", item.id)
        return True

    messages: list[dict[str, Any]] = []
    emit("started", "授权与限制已固定。")
    try:
        for i, text in enumerate(settings.initial_materials, 1):
            if not admit(Evidence(f"E{i}", "initial_material", f"initial_materials[{i - 1}]", text)):
                return finish("limits_exhausted", "max_evidence_bytes")
        messages.extend([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({
                "question": settings.question,
                "initial_evidence": [asdict(item) for item in evidence],
                "permitted_test_ids": [test.test_id for test in settings.permitted_tests],
            }, ensure_ascii=False)},
        ])
        while True:
            control.checkpoint()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return finish("timeout", "total_deadline")
            if (
                model_calls >= settings.limits.max_model_calls
                or tool_calls >= settings.limits.max_tool_calls
            ):
                return finish("limits_exhausted", "call_limit")
            request = {
                "model": settings.model,
                "messages": messages,
                "tools": [READ_TOOL, LIST_DIRECTORY_TOOL, SEARCH_TEXT_TOOL, RUN_TEST_TOOL, ASK_USER_TOOL],
                "response_format": {"type": "json_object"},
                "thinking": {"type": "disabled"},
                "max_tokens": 4096,
                "stream": False,
            }
            if not context_fits(request):
                return finish("limits_exhausted", "max_context_bytes")
            model_calls += 1
            emit("model_attempt", f"第 {model_calls} 次模型请求。")
            control.checkpoint()
            try:
                response = client.complete(
                    request,
                    control,
                )
            except ModelError as exc:
                control.checkpoint()
                emit("model_request_failed", exc.code)
                if time.monotonic() >= deadline:
                    return finish("timeout", "total_deadline")
                if not exc.retryable or model_retries_used >= 1:
                    raise
                model_retries_used += 1
                continue
            control.checkpoint()
            model_retries_used = 0
            message = parse_message(response)
            messages.append(message)
            if not message.get("tool_calls"):
                try:
                    report = validate_report(message["content"], evidence)
                    return finish("completed", "valid_report", report)
                except ReportError as exc:
                    control.checkpoint()
                    if report_repairs_used >= 1:
                        return finish("system_failure", "invalid_report: " + str(exc))
                    report_repairs_used += 1
                    contents = json.dumps(
                        {
                            "type": "report_validation_feedback",
                            "error": str(exc),
                            "available_evidence": [
                                {
                                    "evidence_id": item.id,
                                    "kind": item.kind,
                                    "reference_fields": (["evidence_id", "start_line", "end_line"]
                                                         if item.kind == "file" else ["evidence_id"]),
                                    **({"start_line": item.start_line, "end_line": item.end_line}
                                       if item.kind == "file" else {}),
                                }
                                for item in evidence
                            ]
                        },
                        ensure_ascii=False
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": contents,
                        }
                    )
                    emit("report_validation_feedback", contents)
                    continue
            for call in message["tool_calls"]:
                control.checkpoint()
                if not context_fits(request):
                    return finish("limits_exhausted", "max_context_bytes")
                if time.monotonic() >= deadline:
                    return finish("timeout", "total_deadline")
                if (
                    tool_calls >= settings.limits.max_tool_calls
                    or model_calls >= settings.limits.max_model_calls
                ):
                    return finish("limits_exhausted", "call_limit")
                tool_calls += 1
                emit("tool_attempt", f"第 {tool_calls} 次工具请求。")
                control.checkpoint()
                feedback: dict[str, Any]
                arguments: Any = None
                try:
                    function = call["function"]
                    try:
                        arguments = json.loads(function["arguments"])
                    except ValueError as exc:
                        raise ToolError(
                            "invalid_arguments", "工具参数必须是 JSON 对象。"
                        ) from exc
                    evidence_id = f"E{len(evidence) + 1}"
                    if function["name"] == "read_file":
                        data = read_file(settings.authorized_dir, arguments, settings.limits.max_read_bytes)
                        item = Evidence(evidence_id, "file", **data)
                    elif function["name"] == "list_directory":
                        item = list_directory(settings.authorized_dir, arguments, settings.limits, evidence_id)
                    elif function["name"] == "search_text":
                        item = search_text(settings.authorized_dir, arguments, settings.limits, evidence_id)
                    elif function["name"] == "run_test":
                        item = run_test(settings.authorized_dir, arguments, settings.permitted_tests,
                                        evidence_id, settings.limits, control)
                    elif function["name"] == "ask_user":
                        supplement = request_supplement(arguments, input_provider, control, evidence_id, emit,
                                                        settings.authorized_dir, settings.limits)
                        if isinstance(supplement, dict):
                            messages.append({"role": "tool", "tool_call_id": call["id"],
                                             "content": json.dumps(supplement, ensure_ascii=False)})
                            continue
                        item = supplement
                    else:
                        raise ToolError("unknown_tool", "不支持的调查工具。")
                    serialized = evidence_feedback(item)
                    if len(serialized.encode("utf-8")) > settings.limits.max_tool_feedback_bytes:
                        if function["name"] == "ask_user":
                            raise SupplementLimit("max_tool_feedback_bytes", len(serialized.encode("utf-8")),
                                                  settings.limits.max_tool_feedback_bytes)
                        raise ToolError("feedback_limit", "工具反馈含包装后超限，未保存此证据；请缩小范围。")
                    if not admit(item, call["id"]):
                        return finish("limits_exhausted", "max_evidence_bytes")
                    if item.kind == "test":
                        emit("test_result", json.dumps({"tool_call_id": call["id"],
                                                       "evidence_id": item.id, "result": json.loads(item.content)},
                                                      ensure_ascii=False))
                except ToolError as exc:
                    feedback = {"ok": False, "error": exc.code, "message": str(exc)}
                    if call["function"]["name"] == "run_test":
                        requested_id = arguments.get("test_id") if isinstance(arguments, dict) else None
                        feedback.update({
                            "test_id": requested_id[:128] if isinstance(requested_id, str) and requested_id.isascii() else None,
                            "classification": "permission_denied" if exc.code == "permission_denied" else "invalid_request",
                            "started": False, "exit_code": None, "signal": None,
                            "output": "", "output_truncated": False, "cleanup_status": "not_started",
                        })
                    serialized = json.dumps(feedback, ensure_ascii=False)
                    emit("tool_rejected", exc.code)
                    emit("tool_error", json.dumps({"tool_call_id": call["id"], "feedback": feedback},
                                                  ensure_ascii=False))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": serialized,
                    }
                )
                if call["function"]["name"] == "run_test" and json.loads(serialized).get("ok"):
                    test_result = json.loads(item.content)
                    if test_result["cleanup_status"] == "cleanup_failed":
                        return finish("system_failure", "cleanup_failed")
    except SupplementLimit as exc:
        emit("resource_limit", json.dumps(exc.detail, ensure_ascii=False))
        return finish("limits_exhausted", exc.resource)
    except RunStopped as exc:
        return finish("timeout" if exc.reason == "total_deadline" else "user_cancelled", exc.reason)
    except ModelError as exc:
        return finish(
            "timeout" if time.monotonic() >= deadline else "system_failure", exc.code
        )
    except ReportError as exc:
        return finish("system_failure", "invalid_report: " + str(exc))
    except KeyboardInterrupt:
        control.cancellation.cancel()
        return finish("user_cancelled", "user_cancelled")
    except Exception:
        # 外部通信异常可能包含凭据，终止记录只保留分类，不回显异常文本。
        return finish("system_failure", "unexpected_error")
