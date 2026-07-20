#!/usr/bin/env python3
"""Render MAV_DUO waterfall captures (.dat) into SatNOGS-style PNGs.

GR-free on purpose: only numpy + matplotlib, so the server test suite can
exercise it without GNU Radio and operators can re-render by hand:

    python3 waterfall_render.py <capture.dat> [out.png]

.dat format (written by _WaterfallLogger in MAV_DUO.py): repeating
little-endian records of float64 unix-UTC timestamp + FFT_BINS float32 dB
values, fftshifted so bin 0 = -span/2. No header; a partial trailing
record (hard crash mid-write) is dropped on load. The span rides in the
filename as a trailing `_s<hz>` stem token so crash orphans rendered by a
later run — possibly at a different rx_bw — keep the correct frequency
axis; token-less captures (pre-rx_bw files, MAV_ASTROCAST) fall back to
SPAN_HZ.

Rendering is full resolution — one capture row per pixel row, one FFT bin
per pixel column; the image simply grows with capture length instead of
averaging rows to a fixed height. Captures longer than MAX_ROWS_PER_PNG
rows split into sequential parts (<stem>.png, <stem>_p2.png, ...) sharing
one intensity scale, because the Agg canvas caps at 65536 px per dimension
and a single multi-hour image would be unusable anyway. The .dat is read
through a memmap so day-long captures render without loading gigabytes.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

FFT_BINS = 1024
SPAN_HZ = 200_000.0  # fallback span for captures without an `_s<hz>` stem token
ROW_BYTES = 8 + FFT_BINS * 4
MAX_ROWS_PER_PNG = 25_000  # ~42 min per part at ~9.8 rows/s

# Exact-pixel figure layout (margins around the 1:1 data area, in pixels).
IMG_DPI = 100
IMG_LEFT = 76      # y-axis label + ticks
IMG_RIGHT = 110    # colorbar + its label
IMG_TOP = 42       # title
IMG_BOTTOM = 50    # x-axis label + ticks
_SCALE_SAMPLE_ROWS = 4096  # rows sampled for the shared vmin/vmax percentiles


def center_freq_from_env() -> float | None:
    raw = os.environ.get("GSS_RX_FREQ_HZ", "")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def span_from_name(dat_path: str | Path) -> float | None:
    """Span in Hz from the capture's trailing `_s<hz>` stem token, if any."""
    match = re.search(r"_s(\d+)$", Path(dat_path).stem)
    return float(match.group(1)) if match else None


def _open_records(dat_path: Path) -> tuple[np.ndarray | None, int]:
    """Memmap the capture as (n, ROW_BYTES) uint8 records; drops a partial tail."""
    n = dat_path.stat().st_size // ROW_BYTES
    if n == 0:
        return None, 0
    mm = np.memmap(str(dat_path), dtype=np.uint8, mode="r", shape=(n * ROW_BYTES,))
    return mm.reshape(n, ROW_BYTES), n


def _rows_slice(recs: np.ndarray, a: int, b: int) -> np.ndarray:
    """Materialize rows [a, b) as float32 dB values (copies only that slice)."""
    return recs[a:b, 8:].copy().view("<f4").reshape(b - a, FFT_BINS)


def load_rows(dat_path: str | Path) -> tuple[np.ndarray, np.ndarray]:
    """Return (timestamps[n] float64, rows[n, FFT_BINS] float32); drops a partial trailing record."""
    recs, n = _open_records(Path(dat_path))
    if recs is None:
        return np.empty(0, np.float64), np.empty((0, FFT_BINS), np.float32)
    ts = recs[:, :8].copy().view("<f8").ravel()
    rows = _rows_slice(recs, 0, n)
    return ts, rows


def _intensity_scale(recs: np.ndarray, n: int) -> tuple[float, float]:
    """Shared vmin/vmax from an even sample of rows so all parts match."""
    idx = np.unique(np.linspace(0, n - 1, min(n, _SCALE_SAMPLE_ROWS)).astype(int))
    sample = recs[idx, 8:].copy().view("<f4")
    vmin, vmax = np.percentile(sample, [1.0, 99.5])
    return float(vmin), float(vmax)


def _part_path(out: Path, part: int) -> Path:
    return out if part == 1 else out.with_name(f"{out.stem}_p{part}{out.suffix}")


def _render_part(
    rows: np.ndarray,
    *,
    out: Path,
    vmin: float,
    vmax: float,
    elapsed_start: float,
    elapsed_end: float,
    title: str,
    span_hz: float,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n = len(rows)
    width = IMG_LEFT + FFT_BINS + IMG_RIGHT
    height = IMG_TOP + n + IMG_BOTTOM
    fig = plt.figure(figsize=(width / IMG_DPI, height / IMG_DPI), dpi=IMG_DPI)
    try:
        ax = fig.add_axes(
            [IMG_LEFT / width, IMG_BOTTOM / height, FFT_BINS / width, n / height]
        )
        half_khz = span_hz / 2e3
        im = ax.imshow(
            rows,
            aspect="auto",
            interpolation="none",
            cmap="viridis",
            vmin=vmin,
            vmax=vmax,
            extent=(-half_khz, half_khz, elapsed_end, elapsed_start),
        )
        ax.set_xlabel("Frequency offset (kHz)")
        ax.set_ylabel("Elapsed (s)")
        ax.set_title(title, fontsize=10, pad=8)
        cax = fig.add_axes(
            [(IMG_LEFT + FFT_BINS + 22) / width, IMG_BOTTOM / height,
             16 / width, n / height]
        )
        fig.colorbar(im, cax=cax, label="Power (dB)")
        fig.savefig(out)
    finally:
        plt.close(fig)


def render(
    dat_path: str | Path,
    png_path: str | Path | None = None,
    *,
    delete_dat: bool = False,
    center_freq_hz: float | None = None,
    span_hz: float | None = None,
    max_rows_per_png: int = MAX_ROWS_PER_PNG,
) -> Path | None:
    """Render dat_path to full-resolution PNG(s); return the first part's path,
    or None when there is nothing to draw.

    delete_dat removes the input only after every part rendered (or when the
    file holds zero complete rows and is therefore worthless).
    """
    dat_path = Path(dat_path)
    if not dat_path.is_file():
        return None
    recs, n = _open_records(dat_path)
    if recs is None:
        if delete_dat:
            dat_path.unlink(missing_ok=True)
        return None

    ts = recs[:, :8].copy().view("<f8").ravel()
    vmin, vmax = _intensity_scale(recs, n)
    out = Path(png_path) if png_path is not None else dat_path.with_suffix(".png")
    span = float(span_hz) if span_hz else (span_from_name(dat_path) or SPAN_HZ)
    t0 = float(ts[0])
    start = datetime.fromtimestamp(t0, tz=timezone.utc)
    base_title = f"RX waterfall · start {start:%Y-%m-%d %H:%M:%S}Z"
    if center_freq_hz:
        base_title += f" · {center_freq_hz / 1e6:.6f} MHz"
    base_title += f" · span ±{span / 2e3:g} kHz"

    n_parts = (n + max_rows_per_png - 1) // max_rows_per_png
    for part in range(1, n_parts + 1):
        a = (part - 1) * max_rows_per_png
        b = min(part * max_rows_per_png, n)
        title = base_title if n_parts == 1 else f"{base_title} · part {part}/{n_parts}"
        _render_part(
            _rows_slice(recs, a, b),
            out=_part_path(out, part),
            vmin=vmin,
            vmax=vmax,
            elapsed_start=float(ts[a]) - t0,
            elapsed_end=max(float(ts[b - 1]) - t0, float(ts[a]) - t0 + 0.1),
            title=title,
            span_hz=span,
        )

    del recs
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
