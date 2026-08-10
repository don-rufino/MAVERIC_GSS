"""Pure TLE-fetch logic: detection, URL build, parse, validate. No network."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from mav_gss_lib.platform.tracking.fetch import (  # noqa: E402
    detect_identifier,
    celestrak_url,
    parse_gp_csv,
    validate_tle,
)
from mav_gss_lib.platform.tracking.propagation import TrackingError  # noqa: E402

ISS_L1 = "1 25544U 98067A   26001.50000000  .00000000  00000-0  00000-0 0  9990"
ISS_L2 = "2 25544  51.6400   0.0000 0000000   0.0000   0.0000 15.50000000000007"
EPOCH_2026_001_MS = 1767225600000  # ~2026-01-01T00:00Z, close to the TLE epoch

GP_CSV_HEADER = (
    "OBJECT_NAME,OBJECT_ID,EPOCH,MEAN_MOTION,ECCENTRICITY,INCLINATION,"
    "RA_OF_ASC_NODE,ARG_OF_PERICENTER,MEAN_ANOMALY,EPHEMERIS_TYPE,"
    "CLASSIFICATION_TYPE,NORAD_CAT_ID,ELEMENT_SET_NO,REV_AT_EPOCH,"
    "BSTAR,MEAN_MOTION_DOT,MEAN_MOTION_DDOT"
)


def gp_csv(*rows: str) -> str:
    return GP_CSV_HEADER + "\n" + "".join(r + "\n" for r in rows)


def iss_csv_row(name: str = "MAVERIC", epoch: str = "2026-01-01T12:00:00.000000") -> str:
    return f"{name},1998-067A,{epoch},15.5,0,51.64,0,0,0,0,U,25544,999,0,0,0,0"


# What parse_gp_csv emits for iss_csv_row(): same elements as ISS_L1/ISS_L2 in
# sgp4's canonical rendering (zero BSTAR prints 00000+0, fresh checksums).
ISS_EXPORT_L1 = "1 25544U 98067A   26001.50000000  .00000000  00000-0  00000+0 0  9993"
ISS_EXPORT_L2 = "2 25544  51.6400   0.0000 0000000   0.0000   0.0000 15.50000000    09"

# Live CelesTrak CSV row for catalog 100100 (2026-08-11 pull) and its
# independently hand-converted Alpha-5 TLE — external ground truth for the
# above-99999 range where CelesTrak serves no TLE format at all.
ALPHA5_CSV_ROW = (
    "TRANSPORTER-17 OBJECT CC,2026-156CC,2026-08-09T06:25:00.636672,"
    "14.92284995,.00096583,97.7482,121.1911,51.0938,309.1141,0,U,"
    "100100,999,491,.32933349E-3,.3309E-4,0"
)
ALPHA5_L1 = "1 A0100U 26156CC  26221.26736848  .00003309  00000-0  32933-3 0  9996"
ALPHA5_L2 = "2 A0100  97.7482 121.1911 0009658  51.0938 309.1141 14.92284995  4916"
EPOCH_2026_221_MS = 1786256700000  # 2026-08-09T06:25Z, the ALPHA5 row epoch


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
    def test_always_includes_format_csv(self):
        url = celestrak_url("catnr", "25544")
        self.assertIn("CATNR=25544", url)
        self.assertIn("FORMAT=CSV", url)
        self.assertTrue(url.startswith("https://celestrak.org/NORAD/elements/gp.php?"))

    def test_intdes_param_name(self):
        self.assertIn("INTDES=2026-001A", celestrak_url("intdes", "2026-001A"))

    def test_name_percent_encoded(self):
        self.assertIn("NAME=ISS+ZARYA", celestrak_url("name", "ISS ZARYA"))


class ParseGpCsvTests(unittest.TestCase):
    def test_single_row(self):
        blocks = parse_gp_csv(gp_csv(iss_csv_row()))
        self.assertEqual(blocks, [("MAVERIC", ISS_EXPORT_L1, ISS_EXPORT_L2)])

    def test_alpha5_row_matches_hand_conversion(self):
        blocks = parse_gp_csv(gp_csv(ALPHA5_CSV_ROW))
        self.assertEqual(blocks, [("TRANSPORTER-17 OBJECT CC", ALPHA5_L1, ALPHA5_L2)])

    def test_multiple_rows(self):
        blocks = parse_gp_csv(gp_csv(iss_csv_row("OBJ-A"), iss_csv_row("OBJ-B")))
        self.assertEqual([b[0] for b in blocks], ["OBJ-A", "OBJ-B"])

    def test_epoch_without_fractional_seconds(self):
        blocks = parse_gp_csv(gp_csv(iss_csv_row(epoch="2026-01-01T12:00:00")))
        self.assertEqual(blocks, [("MAVERIC", ISS_EXPORT_L1, ISS_EXPORT_L2)])

    def test_malformed_row_dropped(self):
        blocks = parse_gp_csv(gp_csv(iss_csv_row(epoch="not-a-date"), iss_csv_row("OK")))
        self.assertEqual([b[0] for b in blocks], ["OK"])

    def test_header_only_returns_empty(self):
        self.assertEqual(parse_gp_csv(GP_CSV_HEADER + "\n"), [])

    def test_rejects_invalid_query(self):
        self.assertEqual(parse_gp_csv('Invalid query: "INTLDES=2020-025"'), [])

    def test_rejects_no_gp_data(self):
        self.assertEqual(parse_gp_csv("No GP data found"), [])

    def test_rejects_html(self):
        self.assertEqual(parse_gp_csv("<!DOCTYPE html><html>..."), [])


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
    TleFetchSettings, fetch_tle,
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
    GOOD = gp_csv(iss_csv_row()).encode()

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
        self.assertEqual(r.line1, ISS_EXPORT_L1)

    def test_celestrak_alpha5_end_to_end(self):
        r = fetch_tle(TleFetchSettings(identifier="100100"), now_ms=EPOCH_2026_221_MS,
                      http_opener=_FakeOpener(body=gp_csv(ALPHA5_CSV_ROW).encode()), env={})
        self.assertTrue(r.ok)
        self.assertEqual(r.name, "TRANSPORTER-17 OBJECT CC")
        self.assertEqual(r.line1, ALPHA5_L1)
        self.assertEqual(r.line2, ALPHA5_L2)

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

    def test_multiple_candidates_surface_without_picking(self):
        multi = gp_csv(iss_csv_row("OBJ-A"), iss_csv_row("OBJ-B")).encode()
        r = fetch_tle(TleFetchSettings(identifier="2026-001"), now_ms=EPOCH_2026_001_MS,
                      http_opener=_FakeOpener(body=multi), env={})
        self.assertFalse(r.ok)
        self.assertIsNone(r.via)
        self.assertEqual(len(r.candidates), 2)

    def test_both_fail_prefers_celestrak_candidate_detail(self):
        ct = gp_csv(iss_csv_row("C-A"), iss_csv_row("C-B"), iss_csv_row("C-C")).encode()
        st = gp_csv(iss_csv_row("S-A"), iss_csv_row("S-B")).encode()
        def router(url):
            return io.BytesIO(ct if "celestrak" in url else st)
        r = fetch_tle(TleFetchSettings(identifier="2026-001"), now_ms=EPOCH_2026_001_MS,
                      http_opener=_FakeOpener(router=router),
                      env={"SPACETRACK_IDENTITY": "u", "SPACETRACK_PASSWORD": "p"})
        self.assertFalse(r.ok)
        self.assertEqual(len(r.candidates), 3)   # celestrak's candidates win
        self.assertIn("3", r.detail)

    def test_provider_spacetrack_skips_celestrak(self):
        seen = []
        def router(url):
            seen.append(url)
            return io.BytesIO(self.GOOD)
        r = fetch_tle(TleFetchSettings(identifier="25544", provider="spacetrack"),
                      now_ms=EPOCH_2026_001_MS, http_opener=_FakeOpener(router=router),
                      env={"SPACETRACK_IDENTITY": "u", "SPACETRACK_PASSWORD": "p"})
        self.assertTrue(r.ok)
        self.assertEqual(r.via, "spacetrack")
        self.assertFalse(any("celestrak" in u for u in seen))
        self.assertTrue(any("/format/csv" in u for u in seen))

    def test_provider_spacetrack_without_creds_fails_clean(self):
        r = fetch_tle(TleFetchSettings(identifier="25544", provider="spacetrack"),
                      now_ms=EPOCH_2026_001_MS, http_opener=_FakeOpener(body=self.GOOD), env={})
        self.assertFalse(r.ok)
        self.assertIn("credentials", r.detail.lower())

    def test_provider_celestrak_is_default_and_unchanged(self):
        r = fetch_tle(TleFetchSettings(identifier="25544"), now_ms=EPOCH_2026_001_MS,
                      http_opener=_FakeOpener(body=self.GOOD), env={})
        self.assertTrue(r.ok)
        self.assertEqual(r.via, "celestrak")
