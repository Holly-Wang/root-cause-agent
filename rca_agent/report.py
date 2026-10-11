import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True)
class Evidence:
    id: str
    kind: Literal["initial_material", "file"]
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


def validate_report(content: str, evidence: list[Evidence]) -> dict[str, Any]:
    try:
        report = json.loads(content)
    except ValueError as exc:
        raise ReportError("invalid_json") from exc
    required = {"candidate_causes", "verification", "unconfirmed", "repair_suggestions", "next_steps"}
    if not isinstance(report, dict) or set(report) != required:
        raise ReportError("invalid_report_fields")
    by_id = {item.id: item for item in evidence}
    causes = report["candidate_causes"]
    if not isinstance(causes, list) or not causes:
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
        if not _references(verification["evidence"], by_id):
            raise ReportError("missing_verification_evidence")
    for name in ("unconfirmed", "repair_suggestions", "next_steps"):
        if not isinstance(report[name], list):
            raise ReportError("invalid_text_list")
        for item in report[name]:
            _text(item)
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
        if item.kind == "initial_material":
            if set(ref) != {"evidence_id"}:
                raise ReportError("invalid_initial_reference")
        elif (set(ref) != {"evidence_id", "start_line", "end_line"}
              or type(ref["start_line"]) is not int or type(ref["end_line"]) is not int
              or item.start_line is None or item.end_line is None
              or not item.start_line <= ref["start_line"] <= ref["end_line"] <= item.end_line):
            raise ReportError("invalid_reference_range")
    return value


def render_markdown(result: RunResult) -> str:
    lines = ["# 根因分析报告", "", f"运行状态：{result.status}", f"终止原因：{result.reason}",
             f"模型：{result.model}；通信来源：{result.model_source}", "",
             "分析完成和引用有效不代表诊断正确。", ""]
    if result.report:
        lines += ["## 候选原因", ""]
        for cause in result.report["candidate_causes"]:
            lines += [f"- {cause['summary']}", "  支持证据：" + _format_refs(cause["supporting_evidence"]),
                      "  反驳证据：" + (_format_refs(cause["contradicting_evidence"]) or "未取得反驳证据")]
        lines += ["", "## 实际验证", ""]
        for item in result.report["verification"]:
            lines.append(f"- {item['summary']}（{_format_refs(item['evidence'])}）")
        if not result.report["verification"]:
            lines.append("未报告额外验证；本版本仅提供文件读取。")
        for field, title in (("unconfirmed", "未确认事项"), ("repair_suggestions", "修复建议"), ("next_steps", "下一步")):
            lines += ["", "## " + title, ""]
            lines += ["- " + item for item in result.report[field]] or ["未提供。"]
    else:
        lines += ["尚未形成有效的模型结论；以下保留已取得材料。", ""]
    lines += ["", "## 证据快照", ""]
    for item in result.evidence:
        span = f"，行 {item.start_line}–{item.end_line}" if item.kind == "file" else "，用户提供，未经本次测试验证"
        lines += [f"### {item.id}：{item.source}{span}", ""]
        # 用引用块呈现任意文件内容，避免其中的代码围栏破坏报告结构。
        lines += ["> " + line for line in item.content.splitlines()]
        lines.append("")
    lines += ["## 运行计数", "", f"模型尝试：{result.model_calls}；工具请求：{result.tool_calls}；"
              f"实际耗时：{result.elapsed_seconds:.3f} 秒。", ""]
    return "\n".join(lines)


def _format_refs(refs: list[dict[str, Any]]) -> str:
    return "、".join(ref["evidence_id"] + (f" 行 {ref['start_line']}–{ref['end_line']}" if "start_line" in ref else "")
                    for ref in refs)
