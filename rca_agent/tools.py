from pathlib import Path
from typing import Any


class ToolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def read_file(root: Path, arguments: Any, max_bytes: int) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) != {"path", "start_line", "end_line"}:
        raise ToolError("invalid_arguments", "需要 path、start_line 和 end_line。")
    path, start, end = arguments["path"], arguments["start_line"], arguments["end_line"]
    if (not isinstance(path, str) or not path or type(start) is not int
            or type(end) is not int or not 1 <= start <= end or end - start >= 200):
        raise ToolError("invalid_arguments", "路径必须是文本，行范围必须为正整数且至多 200 行。")
    try:
        target = (root / path).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ToolError("file_unavailable", "文件不可用。") from exc
    if not target.is_relative_to(root):
        raise ToolError("permission_denied", "拒绝读取授权范围外的实际路径。")
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
