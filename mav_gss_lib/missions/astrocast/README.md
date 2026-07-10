# Astrocast 0.1 mission (RX-only)

Decodes the 1k2 beacon from the Astrocast 0.1 technology demonstrator
(NORAD 43798, 437.150 MHz). This is the GSS platform's first real third-party
mission: downlink only, no command surface — the TX panel stays empty
and command admission is rejected by the platform.

Protocol (reverse-engineered by Daniel Estévez —
[Decoding Astrocast 0.1](https://destevez.net/2018/12/decoding-astrocast-0-1/),
[New decoders for Astrocast 0.1](https://destevez.net/2019/03/new-decoders-for-astrocast-0-1/)):

| Mode | Modulation | Framing | Contents |
|---|---|---|---|
| Beacon | 1k2 FSK | non-standard FX.25 (AX.25 UI in RS(255,223), NRZ-I; NRZ failsafe) | ASCII `$GPRMC` (dummy NMEA) + `$HK` housekeeping |

The `$HK` sentence carries: 48-bit clock (1/65536-second ticks since
2016-01-01 UTC), system voltage (V), current (mA), temperature (°C),
RSSI (dBm), AFC offset (Hz), and a format-flags byte. These emit as
`beacon.*` parameters via the declarative walker (`mission.yml`).

## Enabling

Switch missions the **switcher** way — never by hand-editing `gss.yml`:

- In the app: Config gear → Mission → **Active Mission** dropdown → confirm.
  The server restarts into astrocast and the page reloads.
- From the terminal: `python3 MAV_WEB.py --mission astrocast`.

Astrocast keeps its own operator config in `gss.astrocast.yml` (seeded on
first run: 437.150 MHz, TLE 43798, `MAV_ASTROCAST.py`). MAVERIC keeps
`gss.yml`. **Do not set `mission.id: astrocast` in `gss.yml`** — plain
launches always run MAVERIC, and a non-default mission running out of
`gss.yml` would overwrite MAVERIC's config on the next save. Use the
dropdown or `--mission` so each mission reads and writes its own file.

The Radio tab supervises the Astrocast flowgraph; Doppler engage/disengage
works unchanged (RX-only — TX tune messages publish but nothing subscribes).

The receiver runs both 1k2 beacon deframers and automatically searches for
the midpoint of the two FSK tones across ±20 kHz of the Doppler-corrected
baseband. It translates the acquired beacon to zero before gr-satellites'
narrow decode filter, preserving weak-signal selectivity. Standalone or
`platform.radio.args` overrides are available when needed:

```bash
python3 MAV_ASTROCAST.py --afc-search-hz 20000 --afc-bias-hz 0
python3 MAV_ASTROCAST.py --disable-afc --afc-bias-hz -8000
```

The fixed-bias form is intended for a measured current offset, not as a
permanent value copied from an old observation.

Cold automatic acquisition requires three consistent estimates (about
0.74 seconds). Start the radio and Doppler before AOS; acquisition during a
beacon can miss that frame's sync and then decode the next transmission. A
current measured `--afc-bias-hz` centres the channel immediately and gives
the best chance of retaining the first frame.

## Offline replay

Real recordings (Daniel Estévez's
[satellite-recordings](https://github.com/daniestevez/satellite-recordings)
repo — `astrocast.wav`) replay at their recorded sample rate:

```bash
cd gnuradio
python3 MAV_ASTROCAST.py --wavfile /path/to/astrocast.wav
```

The flowgraph opens a MAV_DUO-style Qt window (RX spectrum; plus
waterfall, gain slider, and achieved-frequency readout in USRP mode).
Pass `--headless` for scripted or SSH use.

Frames publish on `tcp://127.0.0.1:52001` in the same PDU format the
GSS RX service consumes, so a running dashboard populates from replay.

## Status caveat

The satellite launched 2018-12-03 (SSO-A); SatNOGS lists it alive but
with no recently decoded telemetry. Treat live passes as opportunistic;
recordings are the reliable demo path.
