import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pricing import discounted_total

actual = discounted_total(1000, 10)
expected = 900
if actual != expected:
    print(f"CHECK_FAILED expected={expected} actual={actual}")
    raise SystemExit(10)
print(f"CHECK_PASSED result={actual}")
