import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    max_model_calls: int = 8
    max_tool_calls: int = 12
    total_seconds: float = 120.0
    max_read_bytes: int = 8192
    max_scan_entries: int = 1000
    max_search_bytes: int = 262144
    max_line_bytes: int = 4096
    max_results: int = 100
    max_tool_feedback_bytes: int = 16384
    max_test_output_bytes: int = 8192
    max_context_bytes: int = 262144
    max_evidence_bytes: int = 131072
    max_supplement_attempts: int = 3
    max_supplement_bytes: int = 8192


@dataclass(frozen=True)
class RunConfig:
    question: str
    initial_materials: tuple[str, ...]
    authorized_dir: Path
    model: str
    limits: Limits
    permitted_tests: tuple["PermittedTest", ...] = ()


@dataclass(frozen=True)
class PermittedTest:
    test_id: str
    command: tuple[str, ...]
    timeout_seconds: float
    assertion_failure_exit_codes: tuple[int, ...] = ()


def load_config(source: Path | Mapping[str, Any]) -> RunConfig:
    base = Path.cwd()
    if isinstance(source, Path):
        try:
            base = source.resolve().parent
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError, RuntimeError) as exc:
            raise ConfigError("配置文件路径必须指向可读的 UTF-8 JSON。") from exc
    else:
        data = dict(source)
    if not isinstance(data, dict):
        raise ConfigError("配置必须是 JSON 对象。")
    if set(data) - {"question", "initial_materials", "authorized_dir", "model", "limits", "permitted_tests"}:
        raise ConfigError("配置包含不支持的字段。")
    question = _text(data.get("question"), "question")
    model = _text(data.get("model"), "model")
    directory = _text(data.get("authorized_dir"), "authorized_dir")
    try:
        root = (base / directory).resolve()
        is_directory = root.is_dir()
    except (OSError, ValueError, RuntimeError) as exc:
        raise ConfigError("authorized_dir 路径无法解析。") from exc
    if not is_directory:
        raise ConfigError("authorized_dir 必须是已存在的目录。")
    materials = data.get("initial_materials")
    if not isinstance(materials, list) or not all(isinstance(x, str) and x.strip() for x in materials):
        raise ConfigError("initial_materials 必须是非空文本组成的列表。")
    raw = data.get("limits", {})
    if not isinstance(raw, dict) or set(raw) - set(Limits.__dataclass_fields__):
        raise ConfigError("limits 包含不支持的字段。")
    values: dict[str, Any] = {}
    for name in ("max_model_calls", "max_tool_calls", "max_read_bytes", "max_scan_entries",
                 "max_search_bytes", "max_line_bytes", "max_results", "max_tool_feedback_bytes", "max_test_output_bytes",
                 "max_context_bytes", "max_evidence_bytes", "max_supplement_attempts", "max_supplement_bytes"):
        value = raw.get(name, getattr(Limits(), name))
        if type(value) is not int or value <= 0:
            raise ConfigError(f"limits.{name} 必须是正整数。")
        values[name] = value
    if values["max_tool_feedback_bytes"] < 2048:
        raise ConfigError("limits.max_tool_feedback_bytes 至少为 2048，以容纳状态和错误反馈。")
    seconds = raw.get("total_seconds", Limits().total_seconds)
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        raise ConfigError("limits.total_seconds 必须是有限正数。")
    values["total_seconds"] = float(seconds)
    tests = _permitted_tests(data.get("permitted_tests", {}))
    return RunConfig(question, tuple(materials), root, model, Limits(**values), tests)


def _permitted_tests(raw: Any) -> tuple[PermittedTest, ...]:
    if not isinstance(raw, dict):
        raise ConfigError("permitted_tests 必须是编号到许可调用的映射。")
    tests = []
    for test_id, entry in raw.items():
        if (not isinstance(test_id, str) or not test_id.isascii()
                or not 1 <= len(test_id) <= 128
                or not all(c.isalnum() or c in "_-" for c in test_id)):
            raise ConfigError("测试编号仅接受至多 128 个 ASCII 字母、数字、下划线或连字符。")
        if (not isinstance(entry, dict) or not {"command", "timeout_seconds"} <= set(entry)
                or set(entry) - {"command", "timeout_seconds", "assertion_failure_exit_codes"}):
            raise ConfigError("许可测试需要 command 和 timeout_seconds，可声明断言失败专用退出码。")
        command = entry["command"]
        if (not isinstance(command, list) or not command
                or not all(isinstance(arg, str) and "\0" not in arg for arg in command)):
            raise ConfigError("command 必须是无 NUL 的程序和参数字符串列表。")
        try:
            for arg in command:
                arg.encode("utf-8")
            program = Path(command[0])
            if not program.is_absolute():
                raise ValueError("relative_program")
            program = program.resolve(strict=True)
            if not program.is_file() or not os.access(program, os.X_OK):
                raise ValueError("not_executable")
        except (OSError, ValueError, RuntimeError, UnicodeError) as exc:
            raise ConfigError("command 首项必须是已存在、可执行程序的绝对路径，参数必须是 UTF-8。") from exc
        seconds = entry["timeout_seconds"]
        try:
            valid_seconds = type(seconds) in (int, float) and math.isfinite(seconds) and seconds > 0
        except OverflowError:
            valid_seconds = False
        if not valid_seconds:
            raise ConfigError("测试 timeout_seconds 必须为有限正数。")
        codes = entry.get("assertion_failure_exit_codes", [])
        if (not isinstance(codes, list)
                or not all(type(code) is int and 1 <= code <= 255 for code in codes)
                or len(codes) != len(set(codes))):
            raise ConfigError("断言失败专用退出码必须是不重复的 1–255 整数列表。")
        # 保留获许可的调用路径；改用 resolve 后的 Python 路径会丢失虚拟环境语义。
        tests.append(PermittedTest(test_id, tuple(command), float(seconds), tuple(codes)))
    return tuple(tests)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} 必须是非空文本。")
    return value
