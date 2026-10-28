import argparse
import signal
import sys
from pathlib import Path
from types import FrameType

from .config import ConfigError
from .control import CancellationToken
from .report import RunEvent
from .runtime import run_analysis
from .supplement import StdinInputProvider


def main() -> int:
    parser = argparse.ArgumentParser(description="对授权 Python 材料进行一次根因分析。")
    parser.add_argument("--config", required=True, type=Path, help="JSON 任务配置")
    parser.add_argument("--output", required=True, type=Path, help="保存 run.json 和 report.md 的目录")
    args = parser.parse_args()

    def progress(event: RunEvent) -> None:
        print(f"[{event.kind}] {event.detail}", file=sys.stderr, flush=True)

    cancellation = CancellationToken()

    def request_cancel(signum: int, frame: FrameType | None) -> None:
        cancellation.cancel()

    previous_handler = signal.signal(signal.SIGINT, request_cancel)
    try:
        try:
            result = run_analysis(args.config, on_event=progress, cancellation=cancellation,
                                  input_provider=StdinInputProvider())
        except ConfigError as exc:
            print(f"配置错误：{exc}", file=sys.stderr)
            return 2
        try:
            result.write_to(args.output)
        except (OSError, ValueError):
            print("无法写入报告目录；请使用授权材料目录之外的可写目录。", file=sys.stderr)
            return 2
        print(f"{result.status}：{args.output / 'report.md'}")
        return 0 if result.status == "completed" else 1
    finally:
        signal.signal(signal.SIGINT, previous_handler)


if __name__ == "__main__":
    raise SystemExit(main())
