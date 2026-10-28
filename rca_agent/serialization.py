"""宿主尺寸准入与实际传输使用同一 UTF-8 JSON 表示。"""

import json
from typing import Any


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode("utf-8")
