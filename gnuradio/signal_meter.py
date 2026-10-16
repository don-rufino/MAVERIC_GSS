"""Per-beacon spectral measurement for the RX display stream.

Pure numpy (no GNU Radio import) so the measurement can be unit-tested and
replayed against SigMF captures offline. MAV_DUO's `_SignalMeter` block feeds
it the display tap (the same stream the Qt spectrum plot shows) and asks for a
measurement each time gr-satellites emits a decoded frame.

Spectrum rows are 1024-point Blackman-Harris FFTs normalized so a full-scale
complex tone reads 0 dBFS. For each decoded frame the meter finds the
strongest stretch of rows whose length matches the frame's time on air inside
a short lookback (the frame PDU arrives after the burst, with an unknown
buffering delay), averages them, and reads:

  peak            highest in-band bin (dBFS)
  noise floor     median in-band bin over the clean second before the burst
  SNR             peak SNR (peak - floor) and -3 dB SNR (peak - 3 dB - floor),
                  the figure operators usually report
  -3 dB           outer edges (both FSK tones) and the strongest tone's width
  -10 / -20 dB    edges and width where the spectrum first drops below
                  peak - 10 / peak - 20 dB, walking outward from the -3 dB
                  outer edges (so they always contain the -3 dB span);
                  a level whose line sits within NEAR_FLOOR_GUARD_DB of the
                  noise floor is not measured (status near_floor)
  C/N0, Eb/N0     excess power over the floor around the signal center
  max hold        the same peak and floor read from a per-bin max-hold of
                  the same rows, matching a max-hold spectrum display read
                  with a cursor (noise max-hold rises with the number of
                  FFTs held, so that count is reported too)
  tune            when the nominal RX frequency is known: the tuned
                  frequency averaged over the burst (Doppler retunes land
                  through note_tune), the received center (tuned + center
                  offset) and its offset from nominal

Rows recorded while the station transmits (reported by the PTT gate through
note_tx) are never used, so an uplink just before a beacon cannot inflate the
floor; when no clean pre-burst rows exist the floor falls back to bins beside
the signal and the result says so.
"""

from __future__ import annotations

import threading
from collections import deque

import numpy as np

FFT_SIZE = 1024
HISTORY_S = 8.0
LOOKBACK_S = 1.5            # PDU arrives after the burst by an unknown delay
FLOOR_WINDOW_S = 1.0        # clean rows wanted before the burst
FLOOR_GAP_S = 0.1           # skip rows right at key-up
FLOOR_MAX_REACH_S = 5.0     # how far back to look for clean rows
TX_GUARD_BEFORE_S = 0.3     # row timestamps lag the antenna by buffering
TX_GUARD_AFTER_S = 0.8      # ... and the receiver needs time to recover
SCHEMA_VERSION = 1
DOPPLER_MIN_SHIFT_HZ = 1.0  # a Doppler "retune" that sits on nominal is a park
BW_LEVELS_DB = (10, 20)     # bandwidth below the peak, beside the -3 dB one
NEAR_FLOOR_GUARD_DB = 3.0   # a level line closer than this to the floor measures noise


def _blackman_harris(n: int) -> np.ndarray:
    k = np.arange(n)
    a = (0.35875, 0.48829, 0.14128, 0.01168)
    return (a[0] - a[1] * np.cos(2 * np.pi * k / (n - 1))
            + a[2] * np.cos(4 * np.pi * k / (n - 1))
            - a[3] * np.cos(6 * np.pi * k / (n - 1)))


def _db(x):
    return 10.0 * np.log10(np.maximum(x, 1e-30))


class SignalMeter:
    """Rolling spectrum history plus the per-frame measurement."""

    def __init__(self, samp_rate: float, fft_size: int = FFT_SIZE,
                 history_s: float = HISTORY_S, nominal_hz: float | None = None) -> None:
        self.fs = float(samp_rate)
        self.nominal_hz = float(nominal_hz) if nominal_hz else None
        self.n = int(fft_size)
        self.win = _blackman_harris(self.n).astype(np.float32)
        self.norm = float(np.sum(self.win)) ** 2
        self.rbw = self.fs / self.n
        self.enbw = self.fs * float(np.sum(self.win ** 2)) / self.norm
        self.freqs = (np.arange(self.n) - self.n // 2) * self.rbw
        self.row_s = self.n / self.fs
        max_rows = max(16, int(history_s / self.row_s))
        self._rows: deque[tuple[float, np.ndarray]] = deque(maxlen=max_rows)
        self._buf = np.empty(0, dtype=np.complex64)
        self._tx: deque[list[float]] = deque(maxlen=64)
        # (wall time, tuned Hz, source) — source "doppler" or "manual"
        self._tunes: deque[tuple[float, float, str]] = deque(maxlen=4 * int(history_s + 4))
        self._lock = threading.Lock()

    # ------------------------------------------------------------ ingest

    def push(self, samples: np.ndarray, now: float) -> None:
        """Add samples that finished arriving at wall time `now`."""
        buf = np.concatenate((self._buf, np.asarray(samples, dtype=np.complex64)))
        nrows = buf.size // self.n
        if nrows:
            block = buf[:nrows * self.n].reshape(nrows, self.n)
            spec = np.fft.fft(block * self.win, axis=1)
            power = np.fft.fftshift((spec.real ** 2 + spec.imag ** 2) / self.norm, axes=1)
            left = buf.size - nrows * self.n
            with self._lock:
                for i in range(nrows):
                    # end time of row i: later samples arrived after it
                    after = (nrows - 1 - i) * self.n + left
                    self._rows.append((now - after / self.fs, power[i].astype(np.float32)))
        self._buf = buf[nrows * self.n:]

    def note_tx(self, start: float, end: float) -> None:
        """Mark [start, end] (wall time) as transmit; update if start matches."""
        with self._lock:
            for window in self._tx:
                if window[0] == start:
                    window[1] = max(window[1], end)
                    return
            self._tx.append([start, end])

    def note_tune(self, t: float, frequency_hz: float, source: str = "doppler") -> None:
        """Record that the receiver was retuned to `frequency_hz` at wall time `t`."""
        with self._lock:
            self._tunes.append((float(t), float(frequency_hz), source))

    def _tune_over(self, t0: float, t1: float) -> dict | None:
        """Tuned frequency over [t0, t1]: time-weighted mean, change across the
        window, and whether a Doppler retune away from nominal was live."""
        if self.nominal_hz is None:
            return None
        with self._lock:
            tunes = list(self._tunes)
        # (start, Hz, source) segments; before the first retune: nominal
        segments = [(float("-inf"), self.nominal_hz, "nominal")]
        for t, hz, source in tunes:
            if t <= t0:
                segments = [(t, hz, source)]
            elif t < t1:
                segments.append((t, hz, source))
        acc = 0.0
        doppler = False
        for i, (start, hz, source) in enumerate(segments):
            a = max(start, t0)
            b = segments[i + 1][0] if i + 1 < len(segments) else t1
            acc += hz * max(0.0, b - a)
            if source == "doppler" and abs(hz - self.nominal_hz) >= DOPPLER_MIN_SHIFT_HZ:
                doppler = True
        span = t1 - t0
        tuned = acc / span if span > 0 else segments[-1][1]
        return {
            "nominal_hz": round(self.nominal_hz, 1),
            "tuned_hz": round(float(tuned), 1),
            "tuned_change_hz": round(float(segments[-1][1] - segments[0][1]), 1),
            "doppler": doppler,
        }

    # ------------------------------------------------------------ measure

    def _tx_mask(self, times: np.ndarray) -> np.ndarray:
        mask = np.zeros(times.size, dtype=bool)
        for start, end in self._tx:
            mask |= (times >= start - TX_GUARD_BEFORE_S) & (times <= end + TX_GUARD_AFTER_S)
        return mask

    def measure(self, air_s: float, baud: float, now: float) -> dict | None:
        """Measure the burst that just produced a decoded frame.

        Returns a JSON-ready dict, or None when there is no spectrum history
        yet. A frame that can only have come from rows recorded while
        transmitting returns {"status": "during_tx"}.
        """
        with self._lock:
            if not self._rows:
                return None
            times = np.fromiter((t for t, _ in self._rows), dtype=np.float64, count=len(self._rows))
            rows = np.stack([r for _, r in self._rows])
            tx = self._tx_mask(times)

        half_bw = min(0.48 * self.fs, max(10e3, 1.25 * baud))
        band = np.abs(self.freqs) <= half_bw
        n_air = max(3, int(round(air_s / self.row_s)))

        cand = np.flatnonzero(times >= now - air_s - LOOKBACK_S)
        if cand.size < n_air:
            return None
        inband = rows[:, band].sum(axis=1)
        best, best_i = -1.0, None
        for i in range(cand[0], cand[-1] - n_air + 2):
            if tx[i:i + n_air].any():
                continue
            p = float(inband[i:i + n_air].mean())
            if p > best:
                best, best_i = p, i
        if best_i is None:
            return {"v": SCHEMA_VERSION, "status": "during_tx"}

        # FSK is constant-envelope, so the best window alone lands on an
        # arbitrary stretch of the burst (often a one-tone run of zeros).
        # Grow it over the whole burst: rows whose in-band power stays above
        # the midpoint between the noise level and the burst level.
        quiet = ~tx & (times < times[best_i] - FLOOR_GAP_S)
        p_noise = float(np.median(inband[quiet])) if quiet.any() else best / 2.0
        thr = p_noise + 0.5 * (best - p_noise)
        a, b = best_i, best_i + n_air - 1
        while a - 1 >= cand[0] and not tx[a - 1] and inband[a - 1] >= thr:
            a -= 1
        while b + 1 < times.size and not tx[b + 1] and inband[b + 1] >= thr:
            b += 1
        sig = rows[a:b + 1].mean(axis=0)
        t0, t1 = times[a] - self.row_s, times[b]

        floor_method = "pre_burst"
        tx_affected = False
        clean = ~tx & (times <= t0 - FLOOR_GAP_S)
        near = clean & (times >= t0 - FLOOR_GAP_S - FLOOR_WINDOW_S)
        want = max(3, int(0.3 * FLOOR_WINDOW_S / self.row_s))
        if near.sum() >= want:
            pick = near
        else:
            tx_affected = bool((tx & (times >= t0 - FLOOR_GAP_S - FLOOR_WINDOW_S) & (times <= t0)).any())
            far = clean & (times >= t0 - FLOOR_MAX_REACH_S)
            pick = far if far.sum() >= want else None
            if pick is not None:
                idx = np.flatnonzero(pick)[-max(want, int(FLOOR_WINDOW_S / self.row_s)):]
                pick = np.zeros_like(near)
                pick[idx] = True
                floor_method = "pre_burst_earlier"
        if pick is not None:
            floor_bin = float(np.median(rows[pick].mean(axis=0)[band]))
            floor_hold = float(np.median(rows[pick].max(axis=0)[band]))
            floor_ffts = int(pick.sum())
        else:
            # beside the signal: in-band bins outside the occupied +-0.75*baud
            beside = band & (np.abs(self.freqs) > 0.75 * baud)
            hold = rows[a:b + 1].max(axis=0)
            floor_bin = float(np.median(sig[beside] if beside.any() else sig[band]))
            floor_hold = float(np.median(hold[beside] if beside.any() else hold[band]))
            floor_ffts = b + 1 - a
            floor_method = "beside_signal"

        bins = np.flatnonzero(band)
        pk = bins[int(np.argmax(sig[band]))]
        half = sig[pk] / 2.0
        above = bins[sig[bins] >= half]
        lo, hi = int(above.min()), int(above.max())
        t_lo, t_hi = pk, pk                       # strongest tone: contiguous run
        while t_lo - 1 >= bins[0] and sig[t_lo - 1] >= half:
            t_lo -= 1
        while t_hi + 1 <= bins[-1] and sig[t_hi + 1] >= half:
            t_hi += 1
        center = (self.freqs[lo] + self.freqs[hi]) / 2.0

        occ = band & (np.abs(self.freqs - center) <= 0.625 * baud)
        excess = float(np.clip(sig[occ] - floor_bin, 0, None).sum()) * self.rbw / self.enbw
        n0 = floor_bin / self.enbw
        cn0 = _db(excess / n0) if excess > 0 else None

        peak_db = float(_db(sig[pk]))
        floor_db = float(_db(floor_bin))
        peak_hold_db = float(_db(rows[a:b + 1].max(axis=0)[band].max()))
        floor_hold_db = float(_db(floor_hold))
        bw_levels = {}
        for level_db in BW_LEVELS_DB:
            line = sig[pk] / 10 ** (level_db / 10)
            above_floor = round(float(_db(line)) - floor_db, 1)
            if above_floor < NEAR_FLOOR_GUARD_DB:
                bw_levels[str(level_db)] = {"status": "near_floor", "line_above_floor_db": above_floor}
                continue
            # from the -3 dB outer edges, so a dip between FSK tones cannot
            # cut the width short; contiguous, so noise bumps beyond the skirts stay out
            l_lo, l_hi = lo, hi
            while l_lo - 1 >= bins[0] and sig[l_lo - 1] >= line:
                l_lo -= 1
            while l_hi + 1 <= bins[-1] and sig[l_hi + 1] >= line:
                l_hi += 1
            bw_levels[str(level_db)] = {
                "status": "ok",
                "edges_hz": [round(float(self.freqs[l_lo])), round(float(self.freqs[l_hi]))],
                "bw_hz": round(float(self.freqs[l_hi] - self.freqs[l_lo] + self.rbw)),
                "line_above_floor_db": above_floor,
                "band_limited": bool(l_lo == bins[0] or l_hi == bins[-1]),
            }
        spectrum = np.round(_db(sig[band]) * 10).astype(int)
        out = {
            "v": SCHEMA_VERSION,
            "status": "ok",
            "peak_dbfs": round(peak_db, 1),
            "peak_offset_hz": round(float(self.freqs[pk])),
            "floor_dbfs": round(floor_db, 1),
            "snr_peak_db": round(peak_db - floor_db, 1),
            "snr_3db_db": round(peak_db - 3.0 - floor_db, 1),
            "edges_3db_hz": [round(float(self.freqs[lo])), round(float(self.freqs[hi]))],
            "bw_3db_hz": round(float(self.freqs[hi] - self.freqs[lo] + self.rbw)),
            "tone_3db_hz": round(float(self.freqs[t_hi] - self.freqs[t_lo] + self.rbw)),
            "bw_levels": bw_levels,
            "center_offset_hz": round(float(center)),
            "maxhold": {
                "peak_dbfs": round(peak_hold_db, 1),
                "floor_dbfs": round(floor_hold_db, 1),
                "snr_peak_db": round(peak_hold_db - floor_hold_db, 1),
                "snr_3db_db": round(peak_hold_db - 3.0 - floor_hold_db, 1),
                "floor_ffts": floor_ffts,
                "burst_ffts": b + 1 - a,
            },
            "cn0_dbhz": None if cn0 is None else round(float(cn0), 1),
            "ebn0_db": None if cn0 is None else round(float(cn0 - _db(baud)), 1),
            "baud": float(baud),
            "floor_method": floor_method,
            "floor_tx_affected": tx_affected,
            "burst_s": round(float(t1 - t0), 3),
            "burst_end_ms": int(t1 * 1000),
            "rbw_hz": round(self.rbw, 2),
            "enbw_hz": round(self.enbw, 2),
            "fft": self.n,
            "window": "blackman-harris",
            "span_hz": self.fs,
            "spectrum": {
                "f0_hz": round(float(self.freqs[bins[0]]), 2),
                "df_hz": round(self.rbw, 4),
                "db10": spectrum.tolist(),
            },
        }
        tune = self._tune_over(t0, t1)
        if tune is not None:
            received = tune["tuned_hz"] + out["center_offset_hz"]
            out["tune"] = tune
            out["received_center_hz"] = round(received, 1)
            out["nominal_offset_hz"] = round(received - tune["nominal_hz"])
        return out


__all__ = ["SignalMeter"]
