import json
import os
import stat
from pathlib import Path
from typing import Any, Iterator, Literal

from .config import Limits
from .report import Evidence, evidence_feedback


class ToolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def read_file(root: Path, arguments: Any, max_bytes: int) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) != {"path", "start_line", "end_line"}:
        raise ToolError("invalid_arguments", "需要 path、start_line 和 end_line。")
    path, start, end = arguments["path"], arguments["start_line"], arguments["end_line"]
    if (not isinstance(path, str) or not path.strip() or type(start) is not int
            or type(end) is not int or not 1 <= start <= end or end - start >= 200):
        raise ToolError("invalid_arguments", "路径必须是文本，行范围必须为正整数且至多 200 行。")
    target = _target(root, path)
    if not target.is_file():
        raise ToolError("file_unavailable", "目标不是普通文件。")
    chunks: list[bytes] = []
    scan_bytes = 0
    retained_bytes = 0
    actual_end = 0
    try:
        with target.open("rb") as handle:
            for number in range(1, end + 1):
                line = handle.readline(262_145 - scan_bytes)
                if not line:
                    break
                scan_bytes += len(line)
                if scan_bytes > 262_144:
                    raise ToolError("scan_limit", "为定位行片段扫描的内容超过 256 KiB。")
                if number >= start:
                    retained_bytes += len(line)
                    if retained_bytes > max_bytes:
                        raise ToolError("read_limit", "片段超过 max_read_bytes，未保存证据；请缩小范围。")
                    chunks.append(line)
                    actual_end = number
        if not chunks:
            raise ToolError("invalid_range", "起始行超出文件范围。")
        content = b"".join(chunks).decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ToolError("file_unavailable", "文件必须是可读的 UTF-8 文本。") from exc
    return {"source": target.relative_to(root).as_posix(), "content": content,
            "start_line": start, "end_line": actual_end}


def _target(root: Path, path: Any) -> Path:
    if not isinstance(path, str) or not path.strip():
        raise ToolError("invalid_arguments", "路径必须是非空白文本，授权根可传入 .。")
    try:
        path.encode("utf-8")
        target = (root / path).resolve(strict=True)
    except (OSError, RuntimeError, ValueError, UnicodeError) as exc:
        raise ToolError("file_unavailable", "文件或目录路径不可用。") from exc
    if not target.is_relative_to(root):
        raise ToolError("permission_denied", "拒绝访问授权范围外的实际路径。")
    return target


_SKIP_REASONS = ("permission_denied", "directory_symlink", "unresolvable_link",
                 "file_unavailable", "non_utf8", "line_limit", "unsupported_type")


class _Discovery:
    """在扫描过程中保留有界快照，并按最终反馈的完整序列化形式准入。"""

    def __init__(self, root: Path, target: Path, path: str, query: str | None,
                 limits: Limits, evidence_id: str) -> None:
        self.root = root
        self.source = target.relative_to(root).as_posix()
        self.path = path
        self.query = query
        self.limits = limits
        self.evidence_id = evidence_id
        self.kind: Literal["directory", "search"] = "directory" if query is None else "search"
        self.rows: list[dict[str, Any]] = []
        self.skipped: list[dict[str, Any]] = []
        self.skipped_reasons: dict[str, int] = {}
        self.skipped_count = 0
        self.skipped_details_omitted = False
        self.scanned_entries = 0
        self.read_bytes = 0
        self.stop_reason: str | None = None
        self.results_truncated = False
        if not self._fits():
            raise ToolError("feedback_limit", "查询范围与最小状态包装超过反馈预算，请缩小请求。")

    def snapshot(self, *, reserve: bool = False) -> Evidence:
        count_bound = self.limits.max_scan_entries + self.limits.max_search_bytes
        data: dict[str, Any] = {
            "path": self.path, "resolved_path": self.source,
            "entries" if self.kind == "directory" else "matches": self.rows,
            "scan_complete": False if reserve else self.stop_reason is None,
            "scanned_entries": self.limits.max_scan_entries if reserve else self.scanned_entries,
            "read_bytes": self.limits.max_search_bytes if reserve else self.read_bytes,
            "returned_count": self.limits.max_results if reserve else len(self.rows),
            "stop_reason": "feedback_limit" if reserve else self.stop_reason,
            "results_truncated": False if reserve else self.results_truncated,
            "skipped_count": count_bound if reserve else self.skipped_count,
            "skipped_reasons": ({reason: count_bound for reason in _SKIP_REASONS}
                                if reserve else self.skipped_reasons),
            "skipped": self.skipped,
            "skipped_details_omitted": False if reserve else self.skipped_details_omitted,
        }
        if self.query is not None:
            data["query"] = self.query
        return Evidence(self.evidence_id, self.kind, self.source,
                        json.dumps(data, ensure_ascii=False))

    def _fits(self) -> bool:
        # 为后续状态、计数字位和固定种类的跳过原因预留空间，不需结束时裁剪。
        return len(evidence_feedback(self.snapshot(reserve=True)).encode("utf-8")) <= self.limits.max_tool_feedback_bytes

    def add(self, row: dict[str, Any]) -> None:
        self.rows.append(row)
        if not self._fits():
            self.rows.pop()
            self.results_truncated = True
            self.stop_reason = "feedback_limit"
        elif len(self.rows) >= self.limits.max_results:
            self.stop_reason = "results_limit"

    def skip(self, path: Path, reason: str, line: int | None = None) -> None:
        self.skipped_count += 1
        self.skipped_reasons[reason] = self.skipped_reasons.get(reason, 0) + 1
        detail: dict[str, Any] = {"path": path.relative_to(self.root).as_posix(), "reason": reason}
        if line is not None:
            detail["line"] = line
        self.skipped.append(detail)
        # 跳过详情最多占反馈预算的四分之一，给正常结果和汇总状态留空间。
        details_bytes = len(json.dumps(json.dumps(self.skipped, ensure_ascii=False),
                                       ensure_ascii=False).encode("utf-8"))
        if details_bytes > self.limits.max_tool_feedback_bytes // 4 or not self._fits():
            self.skipped.pop()
            self.skipped_details_omitted = True


def _walk(target: Path, state: _Discovery) -> Iterator[tuple[Path, Path, str]]:
    try:
        mode = target.stat().st_mode
    except OSError as exc:
        raise ToolError("file_unavailable", "请求目标不可访问。") from exc
    if stat.S_ISREG(mode):
        state.scanned_entries = 1
        yield target, target, "file"
        return
    if not stat.S_ISDIR(mode):
        raise ToolError("file_unavailable", "请求目标必须是普通文件或目录。")
    pending = [target]
    while pending and state.stop_reason is None:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                while state.stop_reason is None:
                    if state.scanned_entries >= state.limits.max_scan_entries:
                        state.stop_reason = "scan_limit"
                        return
                    try:
                        entry = next(entries)
                    except StopIteration:
                        break
                    state.scanned_entries += 1
                    path = Path(entry.path)
                    try:
                        actual = path.resolve(strict=True)
                        if not actual.is_relative_to(state.root):
                            state.skip(path, "permission_denied")
                            continue
                        mode = actual.stat().st_mode
                        is_link = entry.is_symlink()
                    except (OSError, RuntimeError, ValueError):
                        state.skip(path, "unresolvable_link")
                        continue
                    if stat.S_ISDIR(mode):
                        if is_link:
                            state.skip(path, "directory_symlink")
                            continue
                        yield path, actual, "directory"
                        pending.append(actual)
                    elif stat.S_ISREG(mode):
                        yield path, actual, "file_symlink" if is_link else "file"
                    else:
                        state.skip(path, "unsupported_type")
        except OSError as exc:
            if directory == target:
                raise ToolError("file_unavailable", "请求目录不可读取。") from exc
            state.skip(directory, "file_unavailable")


def list_directory(root: Path, arguments: Any, limits: Limits, evidence_id: str) -> Evidence:
    if not isinstance(arguments, dict) or set(arguments) != {"path"}:
        raise ToolError("invalid_arguments", "目录查看只接受 path。")
    target = _target(root, arguments["path"])
    if not target.is_dir():
        raise ToolError("file_unavailable", "目录查看的目标必须是目录。")
    state = _Discovery(root, target, arguments["path"], None, limits, evidence_id)
    for path, _, kind in _walk(target, state):
        state.add({"path": path.relative_to(root).as_posix(), "type": kind})
        if state.stop_reason is not None:
            break
    return state.snapshot()


def _search_file(path: Path, actual: Path, state: _Discovery, *, direct: bool) -> None:
    initial_rows = len(state.rows)
    line_number = 0
    try:
        with actual.open("rb") as handle:
            while state.stop_reason is None:
                remaining = state.limits.max_search_bytes - state.read_bytes
                if remaining <= 0:
                    state.stop_reason = "read_limit"
                    return
                size = min(state.limits.max_line_bytes + 1, remaining)
                raw = handle.readline(size)
                state.read_bytes += len(raw)
                if not raw:
                    return
                line_number += 1
                if len(raw) > state.limits.max_line_bytes:
                    state.skip(path, "line_limit", line_number)
                    while not raw.endswith(b"\n"):
                        remaining = state.limits.max_search_bytes - state.read_bytes
                        if remaining <= 0:
                            state.stop_reason = "read_limit"
                            return
                        raw = handle.readline(min(state.limits.max_line_bytes + 1, remaining))
                        state.read_bytes += len(raw)
                        if not raw:
                            break
                    continue
                if not raw.endswith(b"\n") and len(raw) == size:
                    # 字节预算耗尽时不能把尚未确认完整的行作为行证据。
                    state.stop_reason = "read_limit"
                    return
                text = raw.decode("utf-8")
                if state.query is not None and state.query in text:
                    state.add({"path": path.relative_to(state.root).as_posix(), "line": line_number, "text": text})
    except (OSError, UnicodeError) as exc:
        if direct:
            raise ToolError("file_unavailable", "请求文件必须是可读的 UTF-8 文本。") from exc
        if len(state.rows) > initial_rows:
            del state.rows[initial_rows:]
            state.results_truncated = True
        state.skip(path, "non_utf8" if isinstance(exc, UnicodeError) else "file_unavailable")


def search_text(root: Path, arguments: Any, limits: Limits, evidence_id: str) -> Evidence:
    if not isinstance(arguments, dict) or set(arguments) != {"path", "query"}:
        raise ToolError("invalid_arguments", "文本搜索需要 path 和 query。")
    query = arguments["query"]
    if not isinstance(query, str) or not query:
        raise ToolError("invalid_arguments", "query 必须是非空的字面文本。")
    try:
        query.encode("utf-8")
    except UnicodeError as exc:
        raise ToolError("invalid_arguments", "query 必须是有效 UTF-8 文本。") from exc
    target = _target(root, arguments["path"])
    state = _Discovery(root, target, arguments["path"], query, limits, evidence_id)
    direct = target.is_file()
    for path, actual, kind in _walk(target, state):
        if kind != "directory":
            _search_file(path, actual, state, direct=direct)
        if state.stop_reason is not None:
            break
    return state.snapshot()
