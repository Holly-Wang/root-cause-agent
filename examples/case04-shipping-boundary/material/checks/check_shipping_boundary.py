import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shipping import shipping_fee

actual = shipping_fee(5)
expected = 5
if actual != expected:
    print(f"CHECK_FAILED expected={expected} actual={actual}")
    raise SystemExit(10)
print(f"CHECK_PASSED result={actual}")
