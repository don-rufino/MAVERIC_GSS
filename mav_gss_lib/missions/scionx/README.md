# SCION-X (INSPIRESat-6)

RX-only mission for SCION-X (NCU Taiwan, NORAD 69880): 9k6 2-FSK AX.25 UI
beacons `BN0SCX -> BN0CU` at 437.500 MHz.

Select it in **Config → Mission → SCION-X**. The mission:

- tunes MAV_DUO to 437.500 MHz and tracks NORAD 69880 (TLE seeded; refresh
  in Config → Tracking),
- selects `gnuradio/decoders/SCIONX_DECODER.yml`, which turns on the GSS
  burst decoder in MAV_DUO,
- turns **IQ recording on** the first time it runs on a machine, so every
  pass is kept for decoder work (turning it off and saving sticks). Captures
  land in `<log_dir>/iq/`; recording is skipped when free disk is below
  Config → Radio → Disk reserve.

## Why a GSS burst decoder

Stock gr-satellites cannot deframe SCION-X: the beacons are plain NRZ (its
AX.25 deframer is NRZI-only), the transmitter usually keys up late so the
opening flags and the first ~50-100 bytes are never radiated, and long zero
runs make an adaptive bit clock drift. `gnuradio/scionx_burst.py` handles all
three per burst (fixed bit clock fitted to the whole burst, frame position
from the closing flags and the published ground-test frame, stuffing-choice
scoring, CRC repair on complete frames).

## What the RX list shows

| check | meaning |
|---|---|
| `CRC OK` | complete frame, CRC verified |
| `UNVERIFIED` | frame position found but its start was not received, so there is no CRC; values come from the bytes shown in `bytes` |
| `NO DECODE` | a SCION-X-like burst (9600 bit/s FSK, beacon length) that did not decode; its Signal card and the IQ recording remain |

`AMBIGUOUS` means two stuffing choices tie and values late in the frame may
be one bit off. `IMPLAUSIBLE` lists fields outside physical limits (usually
bit errors on weak bursts).

## Beacon field map (local, not in the repository)

The telemetry layout is team-supplied, so it is not committed. Build the
local map from the team workbook once per machine:

```bash
python3 -m mav_gss_lib.missions.scionx.build_beacon_fields SCIONX-CMD-TLM-Beacon.xlsx
```

This writes the gitignored `beacon_fields.json` beside this file (or copy
that JSON from another GSS machine). Without it frames are still received,
logged and measured, but no telemetry values are extracted and the decoder
loses its plausibility tie-breaker.

## Tuning the decoder on recorded passes

The decoder runs identically offline:

```bash
conda activate radioconda
python3 gnuradio/scionx_burst.py logs/iq/<capture>.sigmf-data
```

Every recorded pass is a replay test: improve the decoder against captures,
then the same code runs live. `tests/test_scionx_burst.py` holds the
synthetic cases (and replays local captures when present).
