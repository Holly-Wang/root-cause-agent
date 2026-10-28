import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .control import CleanupRecord


@dataclass(frozen=True)
class Evidence:
    id: str
    kind: Literal["initial_material", "user_supplement", "file", "directory", "search", "test"]
    source: str
    content: str
    start_line: int | None = None
    end_line: int | None = None


@dataclass(frozen=True)
class RunEvent:
    kind: str
    detail: str


RunStatus = Literal["completed", "limits_exhausted", "timeout", "user_cancelled", "system_failure"]


@dataclass
class RunResult:
    status: RunStatus
    reason: str
    model: str
    evidence: list[Evidence]
    events: list[RunEvent]
    model_calls: int
    tool_calls: int
    elapsed_seconds: float
    model_source: Literal["deepseek", "injected_transport"]
    config_snapshot: dict[str, Any]
    report: dict[str, Any] | None = None
    markdown: str = ""
    cleanup: list[CleanupRecord] = field(default_factory=list)
    cleanup_status: Literal["not_needed", "confirmed", "cleanup_failed"] = "not_needed"
    cleanup_seconds: float = 0.0
    resource_usage: dict[str, int] = field(default_factory=dict)

    def write_to(self, directory: Path) -> None:
        if directory.resolve().is_relative_to(Path(self.config_snapshot["authorized_dir"])):
            raise ValueError("报告目录必须在授权材料目录之外。")
        directory.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data.pop("markdown")
        (directory / "run.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )
        (directory / "report.md").write_text(self.markdown, encoding="utf-8")


class ReportError(ValueError):
    pass


def evidence_feedback(item: Evidence) -> str:
    """工具预算与实际发送共用序列化方式，包含 content 内层 JSON 的转义。"""
    data: dict[str, Any] = {"ok": True, "evidence": asdict(item)}
    if item.kind == "user_supplement":
        data["provenance"] = "user_provided_unverified"
    return json.dumps(data, ensure_ascii=False)


def validate_report(content: str, evidence: list[Evidence]) -> dict[str, Any]:
    try:
        report = json.loads(content)
    except ValueError as exc:
        raise ReportError("invalid_json") from exc
    required = {"root_cause_found", "information_gaps", "follow_up_questions",
                "candidate_causes", "verification", "unconfirmed", "repair_suggestions", "next_steps"}
    if not isinstance(report, dict) or set(report) != required:
        raise ReportError("invalid_report_fields")
    by_id = {item.id: item for item in evidence}
    if type(report["root_cause_found"]) is not bool:
        raise ReportError("invalid_diagnosis_status")
    causes = report["candidate_causes"]
    if not isinstance(causes, list) or (report["root_cause_found"] and not causes):
        raise ReportError("missing_candidate")
    for cause in causes:
        if not isinstance(cause, dict) or set(cause) != {"summary", "supporting_evidence", "contradicting_evidence"}:
            raise ReportError("invalid_candidate")
        _text(cause["summary"])
        supporting = _references(cause["supporting_evidence"], by_id)
        if not any(by_id[ref["evidence_id"]].kind == "file" for ref in supporting):
            raise ReportError("missing_file_evidence")
        _references(cause["contradicting_evidence"], by_id)
    if not isinstance(report["verification"], list):
        raise ReportError("invalid_verification")
    for verification in report["verification"]:
        if not isinstance(verification, dict) or set(verification) != {"summary", "evidence"}:
            raise ReportError("invalid_verification")
        _text(verification["summary"])
        refs = _references(verification["evidence"], by_id)
        if not refs:
            raise ReportError("missing_verification_evidence")
        if any(by_id[ref["evidence_id"]].kind in ("initial_material", "user_supplement") for ref in refs):
            raise ReportError("unverified_user_evidence")
    for name in ("unconfirmed", "repair_suggestions", "next_steps", "information_gaps", "follow_up_questions"):
        if not isinstance(report[name], list):
            raise ReportError("invalid_text_list")
        for item in report[name]:
            _text(item)
    if not report["root_cause_found"] and not all(
        report[name] for name in ("information_gaps", "follow_up_questions", "unconfirmed")
    ):
        raise ReportError("missing_information_gap")
    if report["root_cause_found"] and report["information_gaps"]:
        raise ReportError("inconsistent_diagnosis_status")
    return report


def _text(value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ReportError("invalid_text")


def _references(value: Any, evidence: dict[str, Evidence]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ReportError("invalid_references")
    for ref in value:
        if not isinstance(ref, dict) or not isinstance(ref.get("evidence_id"), str):
            raise ReportError("invalid_reference")
        item = evidence.get(ref["evidence_id"])
        if item is None:
            raise ReportError("unknown_evidence")
        if item.kind != "file":
            if set(ref) != {"evidence_id"}:
                raise ReportError("invalid_non_file_reference")
        elif (set(ref) != {"evidence_id", "start_line", "end_line"}
              or type(ref["start_line"]) is not int or type(ref["end_line"]) is not int
              or item.start_line is None or item.end_line is None
              or not item.start_line <= ref["start_line"] <= ref["end_line"] <= item.end_line):
            raise ReportError("invalid_reference_range")
    return value


def render_markdown(result: RunResult) -> str:
    lines = ["# 根因分析报告", "", f"运行状态：{result.status}", f"终止原因：{result.reason}",
             f"清理状态：{result.cleanup_status}；累计清理耗时：{result.cleanup_seconds:.3f} 秒。",
             f"模型：{result.model}；通信来源：{result.model_source}", "",
             "分析完成和引用有效不代表诊断正确。",
             "许可测试为可信代码，不具备操作系统沙箱隔离。", ""]
    if result.report:
        lines += ["模型声明已找到根因；宿主未证明诊断正确。" if result.report["root_cause_found"]
                  else "尚未找到或确认根因。", ""]
        lines += ["## 候选原因", ""]
        for cause in result.report["candidate_causes"]:
            lines += [f"- {cause['summary']}", "  支持证据：" + _format_refs(cause["supporting_evidence"]),
                      "  反驳证据：" + (_format_refs(cause["contradicting_evidence"]) or "未取得反驳证据")]
        lines += ["", "## 实际验证", ""]
        for item in result.report["verification"]:
            lines.append(f"- {item['summary']}（{_format_refs(item['evidence'])}）")
        if not result.report["verification"]:
            lines.append("未报告额外验证；实际取得的材料见证据快照。")
        for field, title in (("information_gaps", "信息缺口"), ("follow_up_questions", "后续问题"),
                             ("unconfirmed", "未确认事项"), ("repair_suggestions", "修复建议"), ("next_steps", "下一步")):
            lines += ["", "## " + title, ""]
            lines += ["- " + item for item in result.report[field]] or ["未提供。"]
    else:
        lines += ["尚未形成有效的模型结论；以下保留已取得材料。", ""]
    lines += ["", "## 证据快照", ""]
    for item in result.evidence:
        if item.kind == "file":
            span = f"，行 {item.start_line}–{item.end_line}"
        elif item.kind in ("initial_material", "user_supplement"):
            span = "，用户提供，未经本次宿主测试验证"
        elif item.kind == "test":
            span = "，宿主许可测试快照"
        else:
            span = "，宿主目录查看快照" if item.kind == "directory" else "，宿主文本搜索快照"
        lines += [f"### {item.id}：{item.source}{span}", ""]
        # 用引用块呈现任意文件内容，避免其中的代码围栏破坏报告结构。
        lines += ["> " + line for line in item.content.splitlines()]
        lines.append("")
    errors = [event for event in result.events if event.kind == "tool_error"]
    if errors:
        lines += ["## 工具拒绝与故障", ""]
        lines += ["> " + event.detail for event in errors]
        lines.append("")
    inputs = [event for event in result.events if event.kind in ("waiting_for_user", "input_rejected")]
    if inputs:
        lines += ["## 补充等待与拒绝", ""]
        lines += ["> " + event.detail for event in inputs]
        lines.append("")
    if result.cleanup:
        lines += ["## 活动操作清理", ""]
        lines += [f"- {item.operation}：{item.status}，{item.elapsed_seconds:.3f} 秒，"
                  f"进程组 {item.process_group_id}，强杀尝试 {item.force_kill_attempted}。" for item in result.cleanup]
        lines.append("")
    lines += ["## 资源准入", "", json.dumps(result.resource_usage, ensure_ascii=False), ""]
    lines += ["> " + event.detail for event in result.events if event.kind == "resource_limit"]
    lines.append("")
    lines += ["## 运行计数", "", f"模型尝试：{result.model_calls}；工具请求：{result.tool_calls}；"
              f"实际耗时：{result.elapsed_seconds:.3f} 秒。", ""]
    return "\n".join(lines)


def _format_refs(refs: list[dict[str, Any]]) -> str:
    return "、".join(ref["evidence_id"] + (f" 行 {ref['start_line']}–{ref['end_line']}" if "start_line" in ref else "")
                    for ref in refs)
