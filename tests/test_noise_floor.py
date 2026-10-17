"""Per-pass noise floor (server/radio/noise_floor.py).

Seconds are fed as the flowgraph reports them (NOISE_FLOOR lines): floor,
RX gain and the gain steps inside the second. Checks the quiet floor against
bursts, the per-gain split and 5 s gain hold, the antenna's 55 deg stop,
transmit seconds, the LOS / flowgraph-stop records and reading them back.
"""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mav_gss_lib.logging import SessionLog
from mav_gss_lib.server.radio.noise_floor import (
    ADJUSTING, BURST, BURST_DB, GAIN_HOLD_S, QUIET, SERIES_FIELDS, TX,
    NoiseFloorTracker, PassNoiseFloor, recent_passes,
)

AOS_MS = 1_792_262_520_000


def _report(t_s, floor, gain=27.0, changes=()):
    return {"ts_ms": AOS_MS + t_s * 1000, "floor_dbfs": floor, "gain_db": gain,
            "gain_changes": [[AOS_MS + int(c * 1000), g] for c, g in changes], "rbw_hz": 97.66}


def _pass(**kw):
    return PassNoiseFloor(satellite="MAVERIC", station_id="usc", aos_ms=AOS_MS,
                          el_min_deg=5.0, el_max_deg=55.0, station_lat_deg=34.02,
                          station_lon_deg=-118.29, **kw)


def _feed(p, floors, *, gain=27.0, el=30.0, start=0):
    for i, f in enumerate(floors):
        p.add(_report(start + i, f, gain), el, 180.0)


class PassStatisticsTests(unittest.TestCase):
    def test_bursts_are_kept_out_of_the_quiet_floor(self):
        rng = random.Random(1)
        floors = [-101.3 + rng.gauss(0, 0.25) for _ in range(300)]
        for t in range(40, 300, 23):                 # a 2-4 s burst every ~23 s
            for k in range(rng.randint(2, 4)):
                floors[t + k] += rng.uniform(4, 14)
        p = _pass()
        _feed(p, floors)
        g = p.summary(complete=True)["by_gain"][0]
        self.assertAlmostEqual(g["quiet_floor_dbfs"], -101.3, delta=0.15)
        self.assertGreater(g["plain_avg_dbfs"], g["quiet_floor_dbfs"] + 1.0)
        self.assertGreater(g["busy_frac"], 0.05)
        self.assertGreater(g["max_burst_db"], BURST_DB)
        self.assertLessEqual(g["p90_dbfs"] - g["p10_dbfs"], 1.0)

    def test_gain_step_is_adjusting_not_a_burst_and_floors_never_mix(self):
        p = _pass()
        _feed(p, [-101.0] * 180, gain=27.0)
        # slider dragged 27 -> 33 in 1 dB steps across seconds 180-182
        steps = [(180.2, 28), (180.5, 29), (180.9, 30), (181.3, 31), (181.8, 32), (182.4, 33)]
        for t in (180, 181, 182):
            inside = [(c, g) for c, g in steps if t <= c < t + 1]
            p.add(_report(t, -98.0, inside[-1][1], inside), 30.0, 180.0)
        _feed(p, [-95.6] * 200, gain=33.0, start=183)
        out = p.summary(complete=True)
        self.assertEqual([b["gain_db"] for b in out["by_gain"]], [27.0, 33.0])
        self.assertEqual(out["adjusting_s"], 3)
        self.assertEqual(len(out["gain_changes"]), 6)
        self.assertEqual(out["gain_changes"][0], [180.2, 28])
        at33 = out["by_gain"][1]
        self.assertEqual(at33["busy_frac"], 0.0)         # +5.4 dB from the step is no burst
        self.assertEqual(at33["quiet_floor_dbfs"], -95.6)
        self.assertEqual(out["by_gain"][0]["quiet_floor_dbfs"], -101.0)
        states = {row[0]: row[6] for row in out["series"]}
        self.assertEqual([states[t] for t in (179, 180, 181, 182, 183)],
                         [QUIET, ADJUSTING, ADJUSTING, ADJUSTING, QUIET])

    def test_a_gain_counts_only_once_it_holds_five_seconds(self):
        p = _pass()
        _feed(p, [-101.0] * 20, gain=27.0)
        p.add(_report(20, -100.0, 30.0, [(20.5, 30.0)]), 30.0, 180.0)
        _feed(p, [-98.3] * (GAIN_HOLD_S - 1), gain=30.0, start=21)   # held 4 s
        p.add(_report(25, -97.0, 33.0, [(25.5, 33.0)]), 30.0, 180.0)
        live = p.summary(complete=False)
        self.assertEqual([b["gain_db"] for b in live["by_gain"]], [27.0])
        _feed(p, [-95.6] * (GAIN_HOLD_S - 1), gain=33.0, start=26)    # 4 s at 33: not yet
        self.assertNotIn(33.0, [b["gain_db"] for b in p.summary(complete=False)["by_gain"]])
        _feed(p, [-95.6], gain=33.0, start=30)                         # 5th second: counts
        out = p.summary(complete=False)
        self.assertEqual([b["gain_db"] for b in out["by_gain"]], [27.0, 33.0])
        self.assertEqual(out["by_gain"][1]["counted_s"], GAIN_HOLD_S)
        self.assertEqual(out["adjusting_s"], 1 + (GAIN_HOLD_S - 1) + 1)

    def test_antenna_holds_at_its_stop_and_those_seconds_count(self):
        p = _pass()
        for t, el in enumerate([20, 40, 54, 60, 72, 66, 56, 50, 30]):
            p.add(_report(t, -101.0), float(el), 100.0 + t)
        out = p.summary(complete=True)
        self.assertEqual(out["sat_max_el_deg"], 72.0)
        self.assertEqual(out["held_at_max_s"], 4)
        ant = [row[2] for row in out["series"]]
        self.assertEqual(max(ant), 55.0)
        self.assertEqual([row[1] for row in out["series"]][4], 72.0)
        bins = dict((lo, n) for lo, _, n in out["by_gain"][0]["by_elevation"])
        self.assertEqual(bins[50], 6)                   # 54, 4x held at 55, 50
        self.assertEqual(max(bins), 50)

    def test_transmit_seconds_are_excluded_without_breaking_the_run(self):
        p = _pass()
        _feed(p, [-101.0] * 10)
        for t in (10, 11):
            p.add(_report(t, None), 30.0, 180.0)
        _feed(p, [-101.0] * 10, start=12)
        out = p.summary(complete=True)
        self.assertEqual(out["tx_s"], 2)
        self.assertEqual(out["adjusting_s"], 0)
        self.assertEqual(out["by_gain"][0]["counted_s"], 20)
        self.assertEqual(out["series"][10][6], TX)
        self.assertIsNone(out["series"][10][5])

    def test_series_rows_match_their_fields(self):
        p = _pass()
        _feed(p, [-101.0] * 10)
        p.add(_report(10, -92.0), 31.0, 181.0)
        row = p.summary(complete=True)["series"][10]
        self.assertEqual(len(row), len(SERIES_FIELDS))
        record = dict(zip(SERIES_FIELDS, row))
        self.assertEqual(record["t_s"], 10)
        self.assertEqual(record["state"], BURST)
        self.assertEqual(record["burst_db"], 9.0)
        self.assertEqual(record["gain_db"], 27.0)


class _Log:
    def __init__(self):
        self.records = []

    def write_noise_floor_pass(self, record):
        self.records.append(record)


def _tracker(elevations):
    """Tracker whose geometry follows `elevations` (deg per ingested second)."""
    log = _Log()
    station = SimpleNamespace(id="usc", min_elevation_deg=5.0, max_elevation_deg=55.0,
                              lat_deg=34.02, lon_deg=-118.29)
    config = SimpleNamespace(enabled=True, tle=SimpleNamespace(name="MAVERIC", line1="1", line2="2"),
                             selected_station=station)
    runtime = SimpleNamespace(rx=SimpleNamespace(log=log), log_dir="", mission_id="maveric")
    sent = []
    tracker = NoiseFloorTracker(runtime, broadcast=sent.append)
    feed = iter(elevations)
    tracker._geometry = lambda ts_ms: (config, SimpleNamespace(elevation_deg=next(feed), azimuth_deg=200.0))
    return tracker, log, sent


class TrackerTests(unittest.TestCase):
    def test_pass_is_logged_at_los(self):
        els = [1, 3, 5, 12, 30, 12, 6, 4, 2]
        tracker, log, sent = _tracker(els)
        for t in range(len(els)):
            tracker.ingest(_report(t, -101.0))
        self.assertEqual(len(log.records), 1)
        rec = log.records[0]
        self.assertTrue(rec["complete"])
        self.assertEqual(rec["seconds"], 5)              # 5, 12, 30, 12, 6 deg
        self.assertEqual(rec["aos_ms"], AOS_MS + 2000)
        self.assertEqual(len(rec["series"]), 5)
        kinds = [m["type"] for m in sent]
        self.assertEqual(kinds, ["noise_floor"] * 5 + ["noise_floor_pass"])
        self.assertNotIn("series", sent[0]["summary"])
        self.assertIsNone(tracker.live())

    def test_flowgraph_stop_mid_pass_logs_an_incomplete_pass(self):
        tracker, log, _ = _tracker([10, 20, 30])
        for t in range(3):
            tracker.ingest(_report(t, -101.0))
        self.assertEqual(tracker.live()["seconds"], 3)
        tracker.finish(complete=False)
        self.assertEqual(len(log.records), 1)
        self.assertFalse(log.records[0]["complete"])
        tracker.finish(complete=False)                   # nothing open: no second record
        self.assertEqual(len(log.records), 1)

    def test_no_pass_below_min_elevation(self):
        tracker, log, sent = _tracker([1, 2, 3, 2])
        for t in range(4):
            tracker.ingest(_report(t, -101.0))
        tracker.finish()
        self.assertEqual(log.records, [])
        self.assertEqual(sent, [])


class RecentPassesTests(unittest.TestCase):
    def test_reads_logged_passes_newest_first_for_the_mission(self):
        with tempfile.TemporaryDirectory() as tmp:
            def write(mission, aos):
                log = SessionLog(tmp, zmq_addr="tcp://127.0.0.1:52002", version="t",
                                 mission_id=mission, station="GS1", operator="op")
                try:
                    p = PassNoiseFloor(satellite="SAT", station_id="usc", aos_ms=aos,
                                       el_min_deg=5.0, el_max_deg=55.0)
                    for t in range(8):
                        p.add({"ts_ms": aos + t * 1000, "floor_dbfs": -101.0, "gain_db": 27.0,
                               "gain_changes": []}, 20.0, 90.0)
                    log.write_noise_floor_pass(p.summary(complete=True))
                finally:
                    log.close()
                return Path(log.jsonl_path)

            write("maveric", AOS_MS)
            write("suchai4", AOS_MS + 5_000_000)
            write("maveric", AOS_MS + 9_000_000)
            json_dir = Path(tmp) / "json"
            got = recent_passes(json_dir, mission_id="maveric", limit=10)
            self.assertEqual([r["aos_ms"] for r in got], [AOS_MS + 9_000_000, AOS_MS])
            self.assertTrue(all(r["event_id"] and r["session_id"] for r in got))
            self.assertEqual(len(recent_passes(json_dir, mission_id="maveric", limit=1)), 1)

            tracker = NoiseFloorTracker(SimpleNamespace(log_dir=tmp, mission_id="maveric"))
            snap = tracker.snapshot()
            self.assertIsNone(snap["active"])
            self.assertEqual(len(snap["last"]["series"]), 8)
            self.assertTrue(all("series" not in r for r in snap["recent"]))

    def test_record_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = SessionLog(tmp, zmq_addr="tcp://127.0.0.1:52002", version="t",
                             mission_id="maveric", station="GS1", operator="op")
            try:
                p = _pass()
                _feed(p, [-101.0] * 6)
                log.write_noise_floor_pass(p.summary(complete=True))
            finally:
                log.close()
            rec = json.loads(Path(log.jsonl_path).read_text().splitlines()[0])
        self.assertEqual(rec["event_kind"], "noise_floor_pass")
        self.assertEqual(rec["seq"], 0)
        self.assertEqual(rec["mission_id"], "maveric")
        self.assertEqual(rec["noise_floor_pass"]["by_gain"][0]["counted_s"], 6)


if __name__ == "__main__":
    unittest.main()
