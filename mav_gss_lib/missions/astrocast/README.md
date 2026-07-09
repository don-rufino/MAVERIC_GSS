# Astrocast 0.1 mission (RX-only)

Decodes the Astrocast 0.1 technology demonstrator (NORAD 43798,
437.150 MHz). This is the GSS platform's first real third-party
mission: downlink only, no command surface — the TX panel stays empty
and command admission is rejected by the platform.

Protocol (reverse-engineered by Daniel Estévez —
[Decoding Astrocast 0.1](https://destevez.net/2018/12/decoding-astrocast-0-1/),
[New decoders for Astrocast 0.1](https://destevez.net/2019/03/new-decoders-for-astrocast-0-1/)):

| Mode | Modulation | Framing | Contents |
|---|---|---|---|
| Beacon | 1k2 FSK | non-standard FX.25 (AX.25 UI in RS(255,223), NRZ-I; NRZ failsafe) | ASCII `$GPRMC` (dummy NMEA) + `$HK` housekeeping |
| Download | 9k6 FSK | CCSDS Reed-Solomon (1115-byte frames, dual basis, depth-5 interleave) | undocumented — logged raw |

The `$HK` sentence carries: 48-bit clock (1/65536-second ticks since
2016-01-01 UTC), system voltage (V), current (mA), temperature (°C),
RSSI (dBm), AFC offset (Hz), and a format-flags byte. These emit as
`beacon.*` parameters via the declarative walker (`mission.yml`).

## Enabling

In `gss.yml`:

```yaml
mission:
  id: astrocast
platform:
  radio:
    script: gnuradio/MAV_ASTROCAST.py
```

Restart `MAV_WEB.py`. The Radio tab supervises the Astrocast flowgraph;
Doppler engage/disengage works unchanged (RX-only — TX tune messages
publish but nothing subscribes).

## Offline replay

Real recordings (Daniel Estévez's
[satellite-recordings](https://github.com/daniestevez/satellite-recordings)
repo — `astrocast.wav`) decode faster than real time:

```bash
cd gnuradio
python3 MAV_ASTROCAST.py --wavfile /path/to/astrocast.wav
```

Frames publish on `tcp://127.0.0.1:52001` in the same PDU format the
GSS RX service consumes, so a running dashboard populates from replay.

## Status caveat

The satellite launched 2018-12-03 (SSO-A); SatNOGS lists it alive but
with no recently decoded telemetry. Treat live passes as opportunistic;
recordings are the reliable demo path.
