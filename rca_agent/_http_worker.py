"""独立 HTTP 尝试；宿主通过进程生命周期中断 DNS、TLS 和阻塞读写。"""

import http.client
import json
import os
import socket
import sys
from typing import Any


def exchange() -> dict[str, Any]:
    try:
        data = json.load(sys.stdin)
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            return {"error": "missing_api_key"}
        connection = http.client.HTTPSConnection("api.deepseek.com", timeout=data["timeout"])
        try:
            connection.request(
                "POST", "/chat/completions",
                body=data["body"].encode("utf-8"),
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            if response.status != 200:
                return {"error": f"http_{response.status}"}
            body = response.read(1_048_577)
            if len(body) > 1_048_576:
                return {"error": "response_too_large"}
            parsed = json.loads(body)
            if not isinstance(parsed, dict):
                return {"error": "invalid_api_response"}
            return {"response": parsed}
        finally:
            connection.close()
    except socket.timeout:
        return {"error": "request_timeout", "retryable": True}
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError):
        return {"error": "transport_error"}


if __name__ == "__main__":
    print(json.dumps(exchange(), ensure_ascii=False, allow_nan=False), flush=True)
