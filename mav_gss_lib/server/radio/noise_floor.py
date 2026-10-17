"""Per-pass noise floor from the flowgraph's per-second NOISE_FLOOR reports.

The signal meter in MAV_DUO reads the floor of every second (median in-band
bin of the averaged display spectrum, transmit rows excluded) with the RX
gain over that second. This module tags each second with the tracked
satellite's elevation/azimuth, keeps the seconds of one pass (satellite at or
above the station's min_elevation_deg) and, at LOS or when the flowgraph
stops, writes one `noise_floor_pass` record to the session log.

Rules (named constants below):
  - The antenna stops at the station's max_elevation_deg and keeps following
    in azimuth, so antenna elevation = min(satellite elevation, stop). Those
    seconds are valid: the floor is wherever the antenna points.
  - A gain step inside a second makes that second "adjusting". A gain counts
    once it has held GAIN_HOLD_S seconds; shorter holds stay "adjusting".
    Adjusting seconds are kept in the series but never in the statistics.
  - A second is a burst when its floor sits more than BURST_DB above the
    median of the last QUIET_WINDOW_S quiet seconds at the same gain. The
    reference restarts at every gain change, so a gain step is never taken
    for a burst.
  - Statistics are kept per gain; floors at different gains never mix.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING, Any, Callable

from mav_gss_lib.platform.tracking.propagation import build_satellite, look_angles_at, sun_point

if TYPE_CHECKING:
    from mav_gss_lib.server.state import WebRuntime

BURST_DB = 3.0
QUIET_WINDOW_S = 30
GAIN_HOLD_S = 5
MIN_QUIET_FOR_BURSTS = 3     # a run's first quiet seconds seed its reference
ELEVATION_STEP_DEG = 5
RECENT_PASSES = 10

QUIET, BURST, ADJUSTING, TX = 0, 1, 2, 3
SERIES_STATES = ("quiet", "burst", "adjusting", "tx")
SERIES_FIELDS = ("t_s", "sat_el", "ant_el", "az", "gain_db", "floor_dbfs", "state", "burst_db")


def _power_mean_db(values: list[float]) -> float | None:
    if not values:
        return None
    return 10.0 * math.log10(sum(10.0 ** (v / 10.0) for v in values) / len(values))


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1)))]


def _r1(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def sun_elevation_deg(lat_deg: float, lon_deg: float, time_ms: int) -> float:
    """Sun elevation at a site, from the subsolar point (good to ~1 deg)."""
    sun = sun_point(time_ms)
    lat, dec = math.radians(lat_deg), math.radians(sun.lat_deg)
    dlon = math.radians(lon_deg - sun.lon_deg)
    s = math.sin(lat) * math.sin(dec) + math.cos(lat) * math.cos(dec) * math.cos(dlon)
    return math.degrees(math.asin(max(-1.0, min(1.0, s))))


class PassNoiseFloor:
    """The seconds of one pass and the statistics derived from them."""

    def __init__(self, *, satellite: str, station_id: str, aos_ms: int,
                 el_min_deg: float, el_max_deg: float,
                 station_lat_deg: float = 0.0, station_lon_deg: float = 0.0) -> None:
        self.satellite = satellite
        self.station_id = station_id
        self.aos_ms = int(aos_ms)
        self.el_min_deg = float(el_min_deg)
        self.el_max_deg = float(el_max_deg)
        self._lat = float(station_lat_deg)
        self._lon = float(station_lon_deg)
        self.seconds: list[dict[str, Any]] = []
        self.gain_changes: list[list[float]] = []
        self.rbw_hz: float | None = None
        self.predicted_los_ms: int | None = None   # from the GSS pass prediction, for the live axis
        self._run = -1
        self._run_open = False
        self._run_gain: float | None = None
        self._run_len: dict[int, int] = {}
        self._quiet: list[float] = []

    # ------------------------------------------------------------ ingest

    def add(self, report: dict[str, Any], sat_el_deg: float, az_deg: float) -> dict[str, Any]:
        t_ms = int(report["ts_ms"])
        gain = report.get("gain_db")
        floor = report.get("floor_dbfs")
        changes = report.get("gain_changes") or []
        if report.get("rbw_hz") is not None:
            self.rbw_hz = float(report["rbw_hz"])
        for ms, g in changes:
            self.gain_changes.append([round((int(ms) - self.aos_ms) / 1000.0, 1), float(g)])
        sec: dict[str, Any] = {
            "t_ms": t_ms, "sat_el": float(sat_el_deg),
            "ant_el": min(float(sat_el_deg), self.el_max_deg), "az": float(az_deg),
            "gain": None if gain is None else float(gain), "floor": floor,
            "run": None, "burst": False, "above": None, "tx": floor is None,
        }
        if floor is None:
            pass                                    # transmitting: no floor, runs continue
        elif changes:
            self._run_open = False                  # slider moved inside this second
        else:
            if not self._run_open or sec["gain"] != self._run_gain:
                self._run += 1
                self._run_open = True
                self._run_gain = sec["gain"]
                self._quiet = []
            sec["run"] = self._run
            self._run_len[self._run] = self._run_len.get(self._run, 0) + 1
            if len(self._quiet) >= MIN_QUIET_FOR_BURSTS:
                above = float(floor) - median(self._quiet[-QUIET_WINDOW_S:])
                sec["above"] = above
                sec["burst"] = above > BURST_DB
            if not sec["burst"]:
                self._quiet.append(float(floor))
        self.seconds.append(sec)
        return sec

    # ------------------------------------------------------------ derive

    def _state(self, sec: dict[str, Any]) -> int:
        if sec["tx"]:
            return TX
        if sec["run"] is None or self._run_len.get(sec["run"], 0) < GAIN_HOLD_S:
            return ADJUSTING
        return BURST if sec["burst"] else QUIET

    def row(self, sec: dict[str, Any]) -> list[Any]:
        return [
            int(round((sec["t_ms"] - self.aos_ms) / 1000.0)),
            round(sec["sat_el"], 1), round(sec["ant_el"], 1), round(sec["az"]),
            sec["gain"], None if sec["floor"] is None else round(float(sec["floor"]), 1),
            self._state(sec),
            _r1(sec["above"]) if sec["burst"] else None,
        ]

    def _bins(self, secs: list[dict[str, Any]]) -> list[list[Any]]:
        out = []
        lo = int(self.el_min_deg)
        while lo < self.el_max_deg:
            last = lo + ELEVATION_STEP_DEG >= self.el_max_deg
            vals = [float(s["floor"]) for s in secs
                    if not s["burst"] and s["ant_el"] >= lo
                    and (s["ant_el"] < lo + ELEVATION_STEP_DEG or last)]
            if vals:
                out.append([lo, _r1(_power_mean_db(vals)), len(vals),
                            _r1(_percentile(vals, 0.1)), _r1(_percentile(vals, 0.9))])
            lo += ELEVATION_STEP_DEG
        return out

    def summary(self, *, complete: bool, series: bool = True) -> dict[str, Any]:
        counted = [s for s in self.seconds if self._state(s) in (QUIET, BURST)]
        by_gain = []
        for gain in dict.fromkeys(s["gain"] for s in counted):
            secs = [s for s in counted if s["gain"] == gain]
            quiet = [float(s["floor"]) for s in secs if not s["burst"]]
            bursts = [s for s in secs if s["burst"]]
            spans: list[list[int]] = []
            for s in secs:
                t = int(round((s["t_ms"] - self.aos_ms) / 1000.0))
                if spans and t - spans[-1][1] <= 2:
                    spans[-1][1] = t
                else:
                    spans.append([t, t])
            by_gain.append({
                "gain_db": gain,
                "spans_s": spans,
                "counted_s": len(secs),
                "quiet_s": len(quiet),
                "quiet_floor_dbfs": _r1(_power_mean_db(quiet)),
                "p10_dbfs": _r1(_percentile(quiet, 0.1)),
                "p90_dbfs": _r1(_percentile(quiet, 0.9)),
                "plain_avg_dbfs": _r1(_power_mean_db([float(s["floor"]) for s in secs])),
                "busy_frac": round(len(bursts) / len(secs), 3),
                "max_burst_db": _r1(max(s["above"] for s in bursts)) if bursts else None,
                "by_elevation": self._bins(secs),
            })
        last_ms = self.seconds[-1]["t_ms"] if self.seconds else self.aos_ms
        peak = max(self.seconds, key=lambda s: s["sat_el"]) if self.seconds else None
        out: dict[str, Any] = {
            "satellite": self.satellite,
            "station_id": self.station_id,
            "aos_ms": self.aos_ms,
            "los_ms": last_ms,
            "predicted_los_ms": self.predicted_los_ms,
            "complete": bool(complete),
            "el_min_deg": self.el_min_deg,
            "el_max_deg": self.el_max_deg,
            "sat_max_el_deg": _r1(peak["sat_el"]) if peak else None,
            "held_at_max_s": sum(1 for s in self.seconds if s["sat_el"] > self.el_max_deg),
            "sun_el_deg": _r1(sun_elevation_deg(self._lat, self._lon, peak["t_ms"])) if peak else None,
            "gain_hold_s": GAIN_HOLD_S,
            "burst_threshold_db": BURST_DB,
            "quiet_window_s": QUIET_WINDOW_S,
            "rbw_hz": self.rbw_hz,
            "seconds": len(self.seconds),
            "adjusting_s": sum(1 for s in self.seconds if self._state(s) == ADJUSTING),
            "tx_s": sum(1 for s in self.seconds if s["tx"]),
            "gain_changes": self.gain_changes,
            "by_gain": by_gain,
            "series_fields": list(SERIES_FIELDS),
            "series_states": list(SERIES_STATES),
        }
        if series:
            out["series"] = [self.row(s) for s in self.seconds]
        return out


class NoiseFloorTracker:
    """Feeds NOISE_FLOOR reports into the current pass; logs and broadcasts."""

    def __init__(self, runtime: "WebRuntime",
                 broadcast: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.runtime = runtime
        self._broadcast = broadcast
        self._lock = threading.Lock()
        self._current: PassNoiseFloor | None = None
        self._satellite_key: tuple[str, str] | None = None
        self._satellite = None

    def _geometry(self, ts_ms: int):
        tracking = getattr(self.runtime, "tracking", None)
        if tracking is None:
            return None
        config = tracking.config_model()
        if not config.enabled:
            return None
        key = (config.tle.line1, config.tle.line2)
        if key != self._satellite_key:
            self._satellite = build_satellite(config)
            self._satellite_key = key
        look = look_angles_at(self._satellite, config.selected_station, ts_ms)
        return config, look

    def _upcoming(self, from_ms: int, count: int) -> list[dict[str, Any]]:
        tracking = getattr(self.runtime, "tracking", None)
        if tracking is None:
            return []
        try:
            return list(tracking.passes(from_ms=from_ms, count=count).get("passes") or [])
        except Exception:
            logging.exception("noise floor: pass prediction failed")
            return []

    def _predicted_los(self, ts_ms: int) -> int | None:
        for p in self._upcoming(ts_ms - 20 * 60_000, 3):
            if p["aos_ms"] - 60_000 <= ts_ms <= p["los_ms"]:
                return int(p["los_ms"])
        return None

    def next_aos_ms(self, now_ms: int) -> int | None:
        for p in self._upcoming(now_ms, 2):
            if p["aos_ms"] > now_ms:
                return int(p["aos_ms"])
        return None

    def ingest(self, report: dict[str, Any]) -> None:
        try:
            ts_ms = int(report["ts_ms"])
            geometry = self._geometry(ts_ms)
        except Exception:
            logging.exception("noise floor: tracking geometry failed")
            return
        if geometry is None:
            return
        config, look = geometry
        station = config.selected_station
        finished = None
        message = None
        with self._lock:
            cur = self._current
            if cur is not None and (cur.satellite != config.tle.name or cur.station_id != station.id):
                finished, cur, self._current = self._close_locked(complete=False), None, None
            if look.elevation_deg >= station.min_elevation_deg:
                if cur is None:
                    cur = self._current = PassNoiseFloor(
                        satellite=config.tle.name, station_id=station.id, aos_ms=ts_ms,
                        el_min_deg=station.min_elevation_deg, el_max_deg=station.max_elevation_deg,
                        station_lat_deg=station.lat_deg, station_lon_deg=station.lon_deg)
                    cur.predicted_los_ms = self._predicted_los(ts_ms)
                cur.add(report, look.elevation_deg, look.azimuth_deg)
                message = {
                    "type": "noise_floor",
                    "summary": cur.summary(complete=False, series=False),
                    # the last GAIN_HOLD_S rows: an adjusting stretch resolves retroactively
                    "tail": [cur.row(s) for s in cur.seconds[-(GAIN_HOLD_S + 1):]],
                }
            elif cur is not None:
                finished, self._current = self._close_locked(complete=True), None
        if finished is not None:
            self._write(finished)
            message = {"type": "noise_floor_pass", "record": finished}
        if message is not None and self._broadcast is not None:
            self._broadcast(message)

    def _close_locked(self, *, complete: bool) -> dict[str, Any] | None:
        cur = self._current
        if cur is None or not cur.seconds:
            return None
        return cur.summary(complete=complete)

    def finish(self, *, complete: bool = False) -> None:
        """Close the pass in progress (flowgraph stopped / server shutdown)."""
        with self._lock:
            finished, self._current = self._close_locked(complete=complete), None
        if finished is not None:
            self._write(finished)
            if self._broadcast is not None:
                self._broadcast({"type": "noise_floor_pass", "record": finished})

    def _write(self, record: dict[str, Any]) -> None:
        log = getattr(getattr(self.runtime, "rx", None), "log", None)
        if log is None:
            log = getattr(getattr(self.runtime, "tx", None), "log", None)
        if log is None or not hasattr(log, "write_noise_floor_pass"):
            return
        try:
            log.write_noise_floor_pass(record)
        except Exception:
            logging.exception("noise floor pass log failed")

    def live(self) -> dict[str, Any] | None:
        with self._lock:
            return None if self._current is None else self._current.summary(complete=False)

    def snapshot(self, *, limit: int = RECENT_PASSES) -> dict[str, Any]:
        """Live pass (with series), the latest logged pass (with series) and
        the recent logged passes of this mission (summaries only)."""
        recent = recent_passes(Path(self.runtime.log_dir) / "json",
                               mission_id=getattr(self.runtime, "mission_id", None), limit=limit)
        return {"active": self.live(), "last": recent[0] if recent else None,
                "next_aos_ms": self.next_aos_ms(int(time.time() * 1000)), "recent": [
            {k: v for k, v in rec.items() if k != "series"} for rec in recent]}


def recent_passes(json_dir: Path, *, mission_id: str | None, limit: int) -> list[dict[str, Any]]:
    """Newest-first noise_floor_pass records from the session logs."""
    try:
        files = sorted(json_dir.glob("session_*.jsonl"), key=os.path.getmtime, reverse=True)
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for path in files:
        found = []
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if '"noise_floor_pass"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("event_kind") != "noise_floor_pass":
                        continue
                    if mission_id and rec.get("mission_id") != mission_id:
                        continue
                    body = dict(rec.get("noise_floor_pass") or {})
                    body["event_id"] = rec.get("event_id")
                    body["session_id"] = rec.get("session_id")
                    found.append(body)
        except OSError:
            continue
        out.extend(reversed(found))
        if len(out) >= limit:
            break
    out.sort(key=lambda r: r.get("aos_ms") or 0, reverse=True)
    return out[:limit]


__all__ = [
    "BURST_DB", "GAIN_HOLD_S", "QUIET_WINDOW_S", "SERIES_FIELDS", "SERIES_STATES",
    "PassNoiseFloor", "NoiseFloorTracker", "recent_passes", "sun_elevation_deg",
]
