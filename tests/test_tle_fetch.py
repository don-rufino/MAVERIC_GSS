"""Pure TLE-fetch logic: detection, URL build, parse, validate. No network."""
from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from mav_gss_lib.platform.tracking.fetch import (  # noqa: E402
    detect_identifier,
    celestrak_url,
    parse_tle_blocks,
    validate_tle,
)
from mav_gss_lib.platform.tracking.propagation import TrackingError  # noqa: E402

ISS_L1 = "1 25544U 98067A   26001.50000000  .00000000  00000-0  00000-0 0  9990"
ISS_L2 = "2 25544  51.6400   0.0000 0000000   0.0000   0.0000 15.50000000000007"
EPOCH_2026_001_MS = 1767225600000  # ~2026-01-01T00:00Z, close to the TLE epoch


class DetectIdentifierTests(unittest.TestCase):
    def test_norad(self):
        self.assertEqual(detect_identifier("25544"), ("catnr", "25544"))

    def test_norad_looks_like_year(self):
        self.assertEqual(detect_identifier("2019"), ("catnr", "2019"))

    def test_intdes_with_piece(self):
        self.assertEqual(detect_identifier("2026-001A"), ("intdes", "2026-001A"))

    def test_intdes_lowercase_upcased(self):
        self.assertEqual(detect_identifier("2026-001ab"), ("intdes", "2026-001AB"))

    def test_name(self):
        self.assertEqual(detect_identifier("MAVERIC"), ("name", "MAVERIC"))

    def test_all_digit_name_is_catnr(self):
        self.assertEqual(detect_identifier("12345")[0], "catnr")


class CelestrakUrlTests(unittest.TestCase):
    def test_always_includes_format_tle(self):
        url = celestrak_url("catnr", "25544")
        self.assertIn("CATNR=25544", url)
        self.assertIn("FORMAT=TLE", url)
        self.assertTrue(url.startswith("https://celestrak.org/NORAD/elements/gp.php?"))

    def test_intdes_param_name(self):
        self.assertIn("INTDES=2026-001A", celestrak_url("intdes", "2026-001A"))

    def test_name_percent_encoded(self):
        self.assertIn("NAME=ISS+ZARYA", celestrak_url("name", "ISS ZARYA"))


class ParseTleBlocksTests(unittest.TestCase):
    def test_three_line(self):
        text = f"ISS (ZARYA)\n{ISS_L1}\n{ISS_L2}\n"
        self.assertEqual(parse_tle_blocks(text), [("ISS (ZARYA)", ISS_L1, ISS_L2)])

    def test_two_line(self):
        self.assertEqual(parse_tle_blocks(f"{ISS_L1}\n{ISS_L2}"), [("", ISS_L1, ISS_L2)])

    def test_multiple_objects(self):
        text = f"A\n{ISS_L1}\n{ISS_L2}\nB\n{ISS_L1}\n{ISS_L2}"
        self.assertEqual(len(parse_tle_blocks(text)), 2)

    def test_rejects_invalid_query(self):
        self.assertEqual(parse_tle_blocks('Invalid query: "INTLDES=2020-025"'), [])

    def test_rejects_no_gp_data(self):
        self.assertEqual(parse_tle_blocks("No GP data found"), [])

    def test_rejects_html(self):
        self.assertEqual(parse_tle_blocks("<!DOCTYPE html><html>..."), [])


class ValidateTleTests(unittest.TestCase):
    def test_accepts_good_leo(self):
        epoch = validate_tle("ISS", ISS_L1, ISS_L2, now_ms=EPOCH_2026_001_MS)
        self.assertGreater(epoch, 0)

    def test_rejects_seed_99999(self):
        l1 = "1 99999U 26001A   26001.50000000  .00000000  00000-0  15000-3 0  9999"
        l2 = "2 99999  97.8250 154.7171 0058009 348.1000 351.9980 14.91466332000019"
        with self.assertRaises(TrackingError):
            validate_tle("MAVERIC", l1, l2, now_ms=EPOCH_2026_001_MS)

    def test_rejects_stale_epoch(self):
        with self.assertRaises(TrackingError):
            validate_tle("ISS", ISS_L1, ISS_L2, now_ms=EPOCH_2026_001_MS + 60 * 86_400_000)


import io
import urllib.error
from mav_gss_lib.platform.tracking.fetch import (
    FetchResult, TleFetchSettings, fetch_tle,
)


class _FakeOpener:
    """Stand-in for a urllib OpenerDirector — exposes `.open(req, timeout)`."""
    def __init__(self, *, body=b"", error=None, router=None):
        self._body = body
        self._error = error
        self._router = router

    def open(self, req, timeout=None):
        if self._router is not None:
            return self._router(req.full_url)
        if self._error is not None:
            raise self._error
        return io.BytesIO(self._body)


class FetchTleTests(unittest.TestCase):
    GOOD = f"MAVERIC\n{ISS_L1}\n{ISS_L2}\n".encode()

    def test_blank_identifier_skips(self):
        r = fetch_tle(TleFetchSettings(identifier=""), now_ms=EPOCH_2026_001_MS,
                      http_opener=_FakeOpener(body=self.GOOD), env={})
        self.assertFalse(r.ok)
        self.assertIsNone(r.via)
        self.assertIn("no identifier", r.detail.lower())

    def test_celestrak_success(self):
        r = fetch_tle(TleFetchSettings(identifier="25544"), now_ms=EPOCH_2026_001_MS,
                      http_opener=_FakeOpener(body=self.GOOD), env={})
        self.assertTrue(r.ok)
        self.assertEqual(r.via, "celestrak")
        self.assertEqual(r.line1, ISS_L1)

    def test_celestrak_no_gp_data_no_creds_fails_clean(self):
        r = fetch_tle(TleFetchSettings(identifier="99999999"), now_ms=EPOCH_2026_001_MS,
                      http_opener=_FakeOpener(body=b"No GP data found"), env={})
        self.assertFalse(r.ok)
        self.assertIsNone(r.via)

    def test_celestrak_failure_falls_back_to_spacetrack(self):
        def router(url):
            if "celestrak" in url:
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
            return io.BytesIO(self.GOOD)   # space-track login + query
        r = fetch_tle(TleFetchSettings(identifier="25544"), now_ms=EPOCH_2026_001_MS,
                      http_opener=_FakeOpener(router=router),
                      env={"SPACETRACK_IDENTITY": "u", "SPACETRACK_PASSWORD": "p"})
        self.assertTrue(r.ok)
        self.assertEqual(r.via, "spacetrack")

    def test_never_raises_on_network_error(self):
        r = fetch_tle(TleFetchSettings(identifier="25544"), now_ms=EPOCH_2026_001_MS,
                      http_opener=_FakeOpener(error=urllib.error.URLError("boom")),
                      env={})
        self.assertFalse(r.ok)  # no exception escapes
