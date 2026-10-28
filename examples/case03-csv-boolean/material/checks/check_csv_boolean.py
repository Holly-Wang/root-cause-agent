import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from importer import parse_enabled

actual = parse_enabled("false")
expected = False
if actual is not expected:
    print(f"CHECK_FAILED expected={expected} actual={actual}")
    raise SystemExit(10)
print(f"CHECK_PASSED result={actual}")
