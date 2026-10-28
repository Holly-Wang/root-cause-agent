import threading
import time
from dataclasses import dataclass
from typing import Literal


StopReason = Literal["total_deadline", "user_cancelled"]


@dataclass(frozen=True)
class CleanupRecord:
    operation: str
    status: Literal["confirmed", "cleanup_failed"]
    elapsed_seconds: float
    force_kill_attempted: bool
    process_group_id: int


class CancellationToken:
    """调用方可从另一线程请求取消；只保留第一次请求的时间。"""

    def __init__(self) -> None:
        # CLI 信号处理器也会调用 cancel；可重入锁避免中断本线程检查时死锁。
        self._lock = threading.RLock()
        self._requested_at: float | None = None

    def cancel(self) -> None:
        with self._lock:
            if self._requested_at is None:
                self._requested_at = time.monotonic()

    @property
    def requested_at(self) -> float | None:
        with self._lock:
            return self._requested_at


class RunStopped(Exception):
    def __init__(self, reason: StopReason) -> None:
        self.reason = reason
        super().__init__(reason)


class  RunControl:
    def __init__(self, deadline: float, cancellation: CancellationToken) -> None:
        self._deadline = deadline
        self.cancellation = cancellation
        self._reason: StopReason | None = None
        self.cleanup: list[CleanupRecord] = []

    @property
    def deadline(self) -> float:
        return self._deadline

    @property
    def reason(self) -> StopReason | None:
        if self._reason is None:
            cancelled = self.cancellation.requested_at
            if cancelled is not None and cancelled <= self.deadline:
                self._reason = "user_cancelled"
            elif time.monotonic() >= self.deadline:
                self._reason = "total_deadline"
        return self._reason

    def checkpoint(self) -> None:
        reason = self.reason
        if reason is not None:
            raise RunStopped(reason)

    @property
    def remaining(self) -> float:
        self.checkpoint()
        return max(0.0, self.deadline - time.monotonic())

    def wait(self, seconds: float | None = None) -> None:
        """适配器的可中断等待；停止时抛出 RunStopped，调用者先收尾再传播。"""
        remaining = self.remaining
        end = time.monotonic() + (remaining if seconds is None else min(seconds, remaining))
        while True:
            self.checkpoint()
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            # 不在信号处理器内获取 Event 条件锁；轮询间隔也用于真实 I/O 监督。
            time.sleep(min(0.02, remaining))
