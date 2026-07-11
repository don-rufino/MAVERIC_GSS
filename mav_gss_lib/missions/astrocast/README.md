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

The live RF path intentionally mirrors MAV_DUO: B210 A:A/RX2, 1 Msps, gain
40, a +250 kHz parked LO, Doppler commands into the UHD source, the same
coax-relay RX GPIO state, and the same 181-tap decimating FIR feeding a 200
ksps baseband spectrum and waterfall. The live differences are the 437.150
MHz center frequency and three narrow Astrocast 1k2 search branches;
Astrocast remains RX-only and does not instantiate MAV_DUO's TX chain.

The protocol decoder itself is gr-satellites 5.7's native Astrocast 0.1
implementation. `ASTROCAST_DECODER.yml` is its SatYAML with the unrelated 9k6
download entry removed, leaving only the requested 1k2 NRZ-I and legacy NRZ
beacons. gr-satellites performs FSK clock recovery, both Astrocast FX.25
deframers, RS(255,223), and the Astrocast CRC. Before that native decoder,
three frequency-translating channel filters centred at -8, 0, and +8 kHz each
decimate the 200 ksps stream to 20 ksps and perform FM demodulation. Each
branch keeps a narrow 5.5 kHz passband for sensitivity while the bank covers
approximately +/-12 kHz residual carrier error, including SatNOGS' observed
437.141994 MHz placement. A short payload-keyed holdoff removes duplicate
PDUs where adjacent branches overlap. This bypasses gr-satellites' 1.8 kHz
complex-IQ prefilter. The broad spectrum and waterfall still tap the
unchanged MAV_DUO-style 200 ksps stream and use the same timestamp-plus-1024
float-bin capture format and renderer. Engage Doppler before AOS.

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
