import json
import math
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


@dataclass(frozen=True)
class RunConfig:
    question: str
    initial_materials: tuple[str, ...]
    authorized_dir: Path
    model: str
    limits: Limits


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
    if set(data) - {"question", "initial_materials", "authorized_dir", "model", "limits"}:
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
    for name in ("max_model_calls", "max_tool_calls", "max_read_bytes"):
        value = raw.get(name, getattr(Limits(), name))
        if type(value) is not int or value <= 0:
            raise ConfigError(f"limits.{name} 必须是正整数。")
        values[name] = value
    seconds = raw.get("total_seconds", Limits().total_seconds)
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        raise ConfigError("limits.total_seconds 必须是有限正数。")
    values["total_seconds"] = float(seconds)
    return RunConfig(question, tuple(materials), root, model, Limits(**values))


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} 必须是非空文本。")
    return value
