"""Platform/mission boundary guardrail: no MAVERIC-prefixed env-var
literals under ``mav_gss_lib/server/`` or ``mav_gss_lib/platform/``.

Mission-specific env-var names couple platform infrastructure to one
mission's identity. The platform layer must use a generic prefix
(``GSS_``); mission-specific env vars belong under
``mav_gss_lib/missions/<name>/`` only.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = [
    ROOT / "mav_gss_lib" / "platform",
    ROOT / "mav_gss_lib" / "server",
]
PATTERN = re.compile(r'''(?P<q>['"])MAVERIC_[A-Z][A-Z0-9_]*(?P=q)''')


class TestNoMissionEnvVars(unittest.TestCase):
    def test_no_maveric_env_var_literals_in_platform_or_server(self):
        offenders: list[str] = []
        for d in SCAN_DIRS:
            for path in d.rglob("*.py"):
                text = path.read_text()
                for m in PATTERN.finditer(text):
                    line = text[:m.start()].count("\n") + 1
                    offenders.append(f"{path.relative_to(ROOT)}:{line}: {m.group()}")
        self.assertEqual(
            offenders, [],
            "MAVERIC_-prefixed env-var literals found in platform/server "
            "code; rename to GSS_*:\n  " + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
