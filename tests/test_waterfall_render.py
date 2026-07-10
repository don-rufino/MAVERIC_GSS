"""Unit tests for gnuradio/waterfall_render.py — the GR-free .dat → PNG renderer.

No GNU Radio imports: the renderer must stay importable with only
numpy + matplotlib so this suite runs everywhere the server tests run.
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gnuradio"))

import waterfall_render as wr


def _write_rows(path: str, n_rows: int, start_ts: float = 1_700_000_000.0) -> None:
    """Synthesize a capture: noise floor at ~-120 dB with a slowly drifting tone."""
    with open(path, "wb") as fh:
        for i in range(n_rows):
            rng = np.random.default_rng(i)
            row = rng.normal(-120.0, 3.0, wr.FFT_BINS).astype("<f4")
            tone_bin = 512 + int(30 * np.sin(i / 50.0))
            row[tone_bin] = -60.0
            fh.write(struct.pack("<d", start_ts + i / 9.77) + row.tobytes())


class LoadRowsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dat = os.path.join(self.tmp.name, "waterfall_x.dat")

    def test_roundtrip(self):
        _write_rows(self.dat, 5)
        ts, rows = wr.load_rows(self.dat)
        self.assertEqual(rows.shape, (5, wr.FFT_BINS))
        self.assertEqual(len(ts), 5)
        self.assertAlmostEqual(float(ts[0]), 1_700_000_000.0, places=3)
        self.assertLess(float(ts[0]), float(ts[-1]))

    def test_truncated_trailing_record_dropped(self):
        _write_rows(self.dat, 3)
        with open(self.dat, "ab") as fh:
            fh.write(b"\x00" * (wr.ROW_BYTES // 2))  # partial record from a crash
        ts, rows = wr.load_rows(self.dat)
        self.assertEqual(rows.shape, (3, wr.FFT_BINS))

    def test_empty_file(self):
        Path(self.dat).touch()
        ts, rows = wr.load_rows(self.dat)
        self.assertEqual(len(ts), 0)
        self.assertEqual(rows.shape, (0, wr.FFT_BINS))


class FullResolutionTests(unittest.TestCase):
    """One capture row = one pixel row; long captures scroll into parts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dat = os.path.join(self.tmp.name, "waterfall_maveric_20260710T000000Z.dat")

    def _png_size(self, path) -> tuple[int, int]:
        from PIL import Image

        with Image.open(path) as img:
            return img.size

    def test_pixel_rows_match_capture_rows(self):
        _write_rows(self.dat, 137)
        png = wr.render(self.dat)
        width, height = self._png_size(png)
        self.assertEqual(width, wr.IMG_LEFT + wr.FFT_BINS + wr.IMG_RIGHT)
        self.assertEqual(height, wr.IMG_TOP + 137 + wr.IMG_BOTTOM)

    def test_long_capture_splits_into_full_res_parts(self):
        _write_rows(self.dat, 120)
        png = wr.render(self.dat, delete_dat=True, max_rows_per_png=50)
        self.assertTrue(str(png).endswith(".png"))
        p2 = Path(str(png)).with_name(Path(str(png)).stem + "_p2.png")
        p3 = Path(str(png)).with_name(Path(str(png)).stem + "_p3.png")
        self.assertTrue(os.path.isfile(png))
        self.assertTrue(p2.is_file())
        self.assertTrue(p3.is_file())
        self.assertEqual(self._png_size(png)[1], wr.IMG_TOP + 50 + wr.IMG_BOTTOM)
        self.assertEqual(self._png_size(p2)[1], wr.IMG_TOP + 50 + wr.IMG_BOTTOM)
        self.assertEqual(self._png_size(p3)[1], wr.IMG_TOP + 20 + wr.IMG_BOTTOM)
        self.assertFalse(os.path.exists(self.dat))  # deleted only after all parts

    def test_short_capture_stays_single_file(self):
        _write_rows(self.dat, 49)
        png = wr.render(self.dat, max_rows_per_png=50)
        p2 = Path(str(png)).with_name(Path(str(png)).stem + "_p2.png")
        self.assertTrue(os.path.isfile(png))
        self.assertFalse(p2.exists())


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dat = os.path.join(self.tmp.name, "waterfall_20260709T000000Z.dat")

    def test_renders_png(self):
        _write_rows(self.dat, 40)
        png = wr.render(self.dat, center_freq_hz=437_575_000.0)
        self.assertIsNotNone(png)
        self.assertTrue(str(png).endswith(".png"))
        self.assertTrue(os.path.isfile(png))
        self.assertGreater(os.path.getsize(png), 5000)
        self.assertTrue(os.path.isfile(self.dat))  # default keeps the .dat

    def test_single_row_renders(self):
        _write_rows(self.dat, 1)
        self.assertIsNotNone(wr.render(self.dat))

    def test_missing_file_returns_none(self):
        self.assertIsNone(wr.render(os.path.join(self.tmp.name, "nope.dat")))

    def test_zero_rows_returns_none_and_deletes_when_asked(self):
        Path(self.dat).touch()
        self.assertIsNone(wr.render(self.dat, delete_dat=True))
        self.assertFalse(os.path.exists(self.dat))

    def test_zero_rows_kept_without_delete_flag(self):
        Path(self.dat).touch()
        self.assertIsNone(wr.render(self.dat))
        self.assertTrue(os.path.exists(self.dat))

    def test_delete_dat_on_success(self):
        _write_rows(self.dat, 10)
        png = wr.render(self.dat, delete_dat=True)
        self.assertTrue(os.path.isfile(png))
        self.assertFalse(os.path.exists(self.dat))

    def test_dat_kept_when_render_fails(self):
        _write_rows(self.dat, 10)
        bad_png = os.path.join(self.tmp.name, "no-such-dir", "out.png")
        with self.assertRaises(OSError):
            wr.render(self.dat, bad_png, delete_dat=True)
        self.assertTrue(os.path.isfile(self.dat))


if __name__ == "__main__":
    unittest.main()
