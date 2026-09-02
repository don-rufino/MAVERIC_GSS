# SUCHAI-4 mission package

RX-only mission for an object believed to be SUCHAI-4 (Universidad de
Chile), NORAD 69911 / "Transporter-17 Object AU" — **officially
unclaimed as of 2026-08-25**. Identity is inferred, not confirmed: the
downlink's frequency (437.250 MHz) and framing (AX100 Mode 5 ASM+Golay,
4k8 FSK, 1600 Hz deviation) match gr-satellites' `SUCHAI-3.yml`
transmitter entry exactly — the closest known predecessor in the same
bus family — but no official catalog claim exists yet. Revisit this
mission's identity if/when the object is officially identified.

Decoded by MAV_DUO using `gnuradio/decoders/SUCHAI4_DECODER.yml`.

## What we actually know

Three real frames captured 2026-08-28 all decode as a consistent
**big-endian CSP v1 header**: `prio=2, src=1, dest=30, dport=20` on every
frame, with only the source-port field varying (56/41/58) — exactly the
pattern expected of a periodic beacon from one node/port. No public
housekeeping payload format exists (same situation as `luojia1`), so
frames log raw with CSP header facts only — see `ax100_rx.Ax100RxPacketOps`.

## What's still unknown

- The satellite's official identity/catalog claim.
- The telecommand ICD (command IDs, argument encoding, addressing) —
  needed before any uplink capability can be added. Do not attempt to
  reverse-engineer or guess at command formats; sending malformed
  commands to someone else's spacecraft is a real risk, not just a bug.
- The housekeeping/telemetry payload layout inside the CSP packet.

## Next steps

This organization is pursuing direct collaboration with the Universidad
de Chile SUCHAI team to obtain a real ICD for both telemetry decoding and
safe uplink command support. Once available:

- Add an `hk_decoder` (see `ax100_rx.HkDecode`) once telemetry fields are
  known, following the `sharjahsat`/`catsat` pattern.
- Add `CommandOps`, `meta_commands`, `verifier_specs`, and a TX framing
  chain to `mission.yml` only once a real telecommand ICD exists.
