import os
import signal
import subprocess
import time
from typing import Callable

from .control import CleanupRecord, RunControl


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # EPERM 等错误不是退出确认。
    return True


def cleanup_process(process: subprocess.Popen[bytes], control: RunControl, operation: str,
                    drain: Callable[[], None] | None = None) -> CleanupRecord:
    """一次活动操作只有一个两秒清理期限；调用者不得再叠加等待宽限。"""
    started = time.monotonic()
    deadline = started + 2.0
    term_sent = forced = False
    status = "cleanup_failed"
    while True:
        try:
            control.reason  # 清理期间也确立主停止原因，但不中断清理。
            process.poll()
            if drain:
                drain()
            if not _group_exists(process.pid) and process.returncode is not None:
                if drain:
                    for _ in range(16):
                        if time.monotonic() >= deadline:
                            break
                        drain()
                status = "confirmed"
                break
            now = time.monotonic()
            if now >= deadline:
                break
            sig = None
            if not term_sent:
                term_sent = True
                sig = signal.SIGTERM
            elif not forced and now - started >= 0.2:
                forced = True
                sig = signal.SIGKILL
            if sig is not None:
                try:
                    os.killpg(process.pid, sig)
                except OSError:
                    pass
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        except KeyboardInterrupt:
            control.cancellation.cancel()
    record = CleanupRecord(operation, "confirmed" if status == "confirmed" else "cleanup_failed",
                           time.monotonic() - started, forced, process.pid)
    control.cleanup.append(record)
    return record
