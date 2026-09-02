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

`SUCHAI4_DECODER.yml` itself has been verified against real RF, not just
the header parser against static fixtures: replaying an independent full
pass (`iq_suchai4_20260813T065056Z`, 2026-08-13, 200 ksps post-FIR IQ)
through the production gr-satellites chain (`_run_decoder()` in
`tests/test_decode_loopback.py`) decoded 3 ASM+Golay frames with the same
`prio=2, src=1, dest=30, dport=20` CSP header — an independent pass, 15
days earlier than the pinned golden frames, decoding the same header
shape. No golden-fixture test has been added for this pass yet (see
`tests/test_golden_iq_replay.py` for the pattern used by other missions);
the `.sigmf-data` recording lives outside this repo, in the sibling GT_MAV
project's `gnu-radio/iq_recordings/`.

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
safe uplink command support.

### To complete the RX (telemetry) chain, need one of:

- Real telemetry field definitions from the Chilean team (preferred).
- SUCHAI Flight Software struct definitions, if SUCHAI-4 runs the same
  open-source onboard framework as prior SUCHAI missions — worth checking
  before any inference work, now that direct collaboration is underway.
- Failing both: statistical inference from a large multi-pass frame
  corpus (dozens+ frames across multiple times of day, since some fields
  vary with eclipse/sunlight) — a fallback, not the preferred path.

Once telemetry fields are known: add an `hk_decoder` (see
`ax100_rx.HkDecode`), following the `sharjahsat`/`catsat` pattern.

### To complete the TX (uplink) chain, need from the Chilean team:

- **Telecommand ICD** — command IDs, argument encoding/byte layout, any
  checksum/CRC scheme on the command body.
- **Addressing/routing** — CSP node map and destination addresses, if
  CSP-based like the `ax100_rx` family here.
- **Uplink RF parameters** — baud/deviation/coding for the uplink, which
  may differ from the downlink already captured.
- **Command verification method** — a telemetry field that echoes the
  last command, or an operational protocol for confirming uplink success.

Do not attempt to reverse-engineer or guess at any of the above; sending
malformed commands to someone else's spacecraft is a real risk, not just
a bug. Once a real ICD exists: add `CommandOps`, `meta_commands`,
`verifier_specs`, and a TX framing chain to `mission.yml`.

### Reminder: file privacy is undecided

Before landing any real ICD-derived data (command IDs, addressing,
argument encoding) into `mission.py` or a new `commands.yml`: decide the
privacy boundary. The `maveric` mission gitignores its `commands.yml`/
`mission.yml` but keeps `mission.py` tracked/public — and that file
currently hardcodes real uplink routing (CSP destination node, dest
port) directly in code, not just in the gitignored data files. Since
SUCHAI-4 involves a third party's ICD rather than this org's own bird,
whether `mission.py` should also stay private (not just the data file)
is an open decision — raise it again when this work actually starts.
