"""Station-wide USRP serial -> name catalog (server/radio/radios.py)."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from mav_gss_lib.server.radio.radios import RADIOS_PATH, RadioCatalog

REPO = Path(__file__).resolve().parents[1]


class RadioCatalogTests(unittest.TestCase):
    def test_seen_names_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "radios.yml"
            cat = RadioCatalog(path)
            self.assertEqual(cat.seen("TESTSN1", "B210"), "")
            cat.set_names({"TESTSN1": "SDR-A", "NOPE": "ignored"})
            again = RadioCatalog(path)
            self.assertEqual(again.names(), {"TESTSN1": "SDR-A"})
            self.assertEqual(again.seen("TESTSN1", "B210"), "SDR-A")
            row = again.snapshot()[0]
            self.assertEqual((row["serial"], row["name"], row["product"]), ("TESTSN1", "SDR-A", "B210"))
            self.assertIsInstance(row["last_seen_ms"], int)

    def test_memory_only_catalog_writes_nothing(self):
        cat = RadioCatalog(None)
        cat.seen("TESTSN1", "B210")
        cat.set_names({"TESTSN1": "SDR-A"})
        self.assertEqual(cat.name("TESTSN1"), "SDR-A")

    def test_unreadable_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "radios.yml"
            path.write_text("radios: [not, a, mapping]\n")
            self.assertEqual(RadioCatalog(path).names(), {})


class RadioCatalogPrivacyTests(unittest.TestCase):
    """USRP serials are private station data: the catalog must never be committed."""

    def test_catalog_file_is_gitignored(self):
        rel = RADIOS_PATH.relative_to(REPO)
        try:
            result = subprocess.run(["git", "check-ignore", "-q", str(rel)], cwd=REPO, timeout=10)
        except (OSError, subprocess.SubprocessError):
            self.skipTest("git not available")
        self.assertEqual(result.returncode, 0, f"{rel} must be gitignored (it holds USRP serials)")

    def test_catalog_file_is_not_tracked(self):
        rel = RADIOS_PATH.relative_to(REPO)
        try:
            result = subprocess.run(["git", "ls-files", "--error-unmatch", str(rel)], cwd=REPO,
                                    capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            self.skipTest("git not available")
        self.assertNotEqual(result.returncode, 0, f"{rel} is tracked by git; remove it from the index")


if __name__ == "__main__":
    unittest.main()
