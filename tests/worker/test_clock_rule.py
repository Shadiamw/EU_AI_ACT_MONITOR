"""Spec 4.5: any direct use of the system time outside the clock module is a defect."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = re.compile(
    r"datetime\.now|datetime\.utcnow|utcnow\(|time\.time\(|time_ns\(|date\.today|"
    r"localtime\(|gmtime\(|from time import time\b"
)


def test_no_direct_system_time_calls_outside_the_clock_module() -> None:
    offenders = []
    for path in sorted((ROOT / "app").rglob("*.py")) + sorted((ROOT / "migrations").rglob("*.py")):
        if path == ROOT / "app" / "clock.py":
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if FORBIDDEN.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert offenders == []
