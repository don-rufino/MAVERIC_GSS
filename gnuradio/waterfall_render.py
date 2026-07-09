#!/usr/bin/env python3
"""Render MAV_DUO waterfall captures (.dat) into SatNOGS-style PNGs.

GR-free on purpose: only numpy + matplotlib, so the server test suite can
exercise it without GNU Radio and operators can re-render by hand:

    python3 waterfall_render.py <capture.dat> [out.png]

.dat format (written by _WaterfallLogger in MAV_DUO.py): repeating
little-endian records of float64 unix-UTC timestamp + FFT_BINS float32 dB
values, fftshifted so bin 0 = -SPAN_HZ/2. No header; a partial trailing
record (hard crash mid-write) is dropped on load.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

FFT_BINS = 1024
SPAN_HZ = 200_000.0  # MAV_DUO decimated RX stream (1 Msps / rx_decim 5)
ROW_BYTES = 8 + FFT_BINS * 4
MAX_IMAGE_ROWS = 8000


def center_freq_from_env() -> float | None:
    raw = os.environ.get("GSS_RX_FREQ_HZ", "")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def load_rows(dat_path: str | Path) -> tuple[np.ndarray, np.ndarray]:
    """Return (timestamps[n] float64, rows[n, FFT_BINS] float32); drops a partial trailing record."""
    raw = np.fromfile(str(dat_path), dtype=np.uint8)
    n = raw.size // ROW_BYTES
    if n == 0:
        return np.empty(0, np.float64), np.empty((0, FFT_BINS), np.float32)
    recs = raw[: n * ROW_BYTES].reshape(n, ROW_BYTES)
    ts = recs[:, :8].copy().view("<f8").ravel()
    rows = recs[:, 8:].copy().view("<f4").reshape(n, FFT_BINS)
    return ts, rows


def reduce_rows(
    ts: np.ndarray, rows: np.ndarray, max_rows: int = MAX_IMAGE_ROWS
) -> tuple[np.ndarray, np.ndarray]:
    """Average adjacent rows so hour-long runs stay under max_rows image rows."""
    n = len(rows)
    if n <= max_rows:
        return ts, rows
    factor = int(np.ceil(n / max_rows))
    m = n // factor
    reduced = rows[: m * factor].reshape(m, factor, rows.shape[1]).mean(axis=1)
    return ts[: m * factor : factor], reduced.astype(np.float32)


def render(
    dat_path: str | Path,
    png_path: str | Path | None = None,
    *,
    delete_dat: bool = False,
    center_freq_hz: float | None = None,
) -> Path | None:
    """Render dat_path to a PNG; return its path, or None when there is nothing to draw.

    delete_dat removes the input only after a successful render (or when the
    file holds zero complete rows and is therefore worthless).
    """
    dat_path = Path(dat_path)
    if not dat_path.is_file():
        return None
    ts, rows = load_rows(dat_path)
    if len(rows) == 0:
        if delete_dat:
            dat_path.unlink(missing_ok=True)
        return None
    ts, rows = reduce_rows(ts, rows)
    out = Path(png_path) if png_path is not None else dat_path.with_suffix(".png")

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    vmin, vmax = np.percentile(rows, [1.0, 99.5])
    total_s = max(float(ts[-1] - ts[0]), 0.1)
    start = datetime.fromtimestamp(float(ts[0]), tz=timezone.utc)
    title = f"RX waterfall · start {start:%Y-%m-%d %H:%M:%S}Z"
    if center_freq_hz:
        title += f" · {center_freq_hz / 1e6:.6f} MHz"

    fig, ax = plt.subplots(figsize=(8, 12), dpi=120)
    half_khz = SPAN_HZ / 2e3
    im = ax.imshow(
        rows,
        aspect="auto",
        cmap="viridis",
        vmin=vmin,
        vmax=vmax,
        extent=(-half_khz, half_khz, total_s, 0.0),
    )
    ax.set_xlabel("Frequency offset (kHz)")
    ax.set_ylabel("Elapsed (s)")
    ax.set_title(title)
    fig.colorbar(im, ax=ax, label="Power (dB)")
    fig.tight_layout()
    try:
        fig.savefig(out)
    finally:
        plt.close(fig)
    if delete_dat:
        dat_path.unlink(missing_ok=True)
    return out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: waterfall_render.py <capture.dat> [out.png]")
    result = render(
        sys.argv[1],
        sys.argv[2] if len(sys.argv) > 2 else None,
        center_freq_hz=center_freq_from_env(),
    )
    print(result if result else "no complete rows; nothing rendered")
