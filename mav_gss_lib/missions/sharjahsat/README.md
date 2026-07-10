# Sharjahsat-1 mission (RX-only)

Decodes the 9k6 housekeeping beacon from SharjahSat-1, the University of
Sharjah / SAASST 3U CubeSat (NORAD 55104, 437.325 MHz). Downlink only,
no command surface — the TX panel stays empty and command admission is
rejected by the platform.

Protocol (community documentation: gr-satellites `Sharjahsat-1.yml`
SatYAML and the satnogs-decoders `sharjahsat1.ksy` Kaitai definition):

| Mode | Modulation | Framing | Contents |
|---|---|---|---|
| Beacon | 9k6 FSK (deviation 3 kHz) | AX.25 G3RUH UI (A60UOS → A62UOS) | `ESER` header + tm_id `'P'` housekeeping / `'A'` image chunks |

The 246-byte `'P'` housekeeping block carries system info (OBC unix
clock, op mode, restart count, uptime, antenna deploy status), OBC
temperatures and rails, battery pack (voltage/current/cell temps), EPS
rails (3.3/5/12 V), ADCS state/attitude/rates, UHF and S-band modem
health, and the solar array (BCR voltages/currents, panel thermistors,
diode output). These emit as `sys.*`, `obc.*`, `batt.*`, `eps.*`,
`adcs.*`, `uhf.*`, `sband.*`, and `solar.*` parameters via the
declarative walker (`mission.yml`). Image packets are logged raw as
opaque products.

Field layout was verified byte-exact against a live frame received by
GS-1 on 2026-07-10 05:37:23 UTC — the frame's interface-board RTC and
OBC unix clock agree to the second, and that frame is the golden
fixture in `tests/test_mission_sharjahsat.py`.

## Enabling

Switch missions the **switcher** way — never by hand-editing `gss.yml`:

- In the app: Config gear → Mission → **Active Mission** dropdown → confirm.
  The server restarts into sharjahsat and the page reloads.
- From the terminal: `python3 MAV_WEB.py --mission sharjahsat`.

Sharjahsat keeps its own operator config in `gss.sharjahsat.yml` (seeded
on first run: 437.325 MHz, TLE 55104, `MAV_DUO.py`). MAVERIC keeps
`gss.yml`. **Do not set `mission.id: sharjahsat` in `gss.yml`** — plain
launches always run MAVERIC, and a non-default mission running out of
`gss.yml` would overwrite MAVERIC's config on the next save. Use the
dropdown or `--mission` so each mission reads and writes its own file.

## Radio path

The mission reuses the stock MAVERIC flowgraph (`gnuradio/MAV_DUO.py`
with `MAVERIC_DECODER.yml`): SharjahSat-1 decodes through the
`9k6 FSK AX.25 G3RUH downlink` branch, proven live on the 2026-07-10
first-light pass. Only the tune changes — the mission seeds
437.325 MHz and the 55104 TLE; Doppler engage/disengage works
unchanged (RX-only — TX tune messages publish but nothing subscribes).
Because SharjahSat-1 has a real catalog number, the in-app CelesTrak
TLE fetch works directly.

## Operational role

SharjahSat-1 transmits reliably, which makes this mission a permanent
known-good calibration target: any pass exercises pointing, Doppler,
gain, decode, and the PDU path end-to-end, with the battery-voltage
column as the pass/fail flag.
