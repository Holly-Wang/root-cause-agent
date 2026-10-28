import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from average import average

actual = average([3, 6, 9])
expected = 6
if actual != expected:
    print(f"CHECK_FAILED expected={expected} actual={actual}")
    raise SystemExit(10)
print(f"CHECK_PASSED result={actual}")
