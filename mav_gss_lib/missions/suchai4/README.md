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

Every frame captured across three independent sessions (2026-08-28,
2026-09-04, 2026-09-11) decodes as a consistent **big-endian CSP v1
header**: `prio=2, src=1, dest=30, dport=20` on every frame, with only
the source-port field varying — exactly the pattern expected of a
periodic beacon from one node/port. `dest=30` is `suchai_4_ground_station`
(the common libcsp convention of reserving 29-31 for the ground segment),
not noise — the shared AX100 parser's dest<=20 plausibility heuristic
doesn't know that, so this mission overrides it locally
(`max_plausible_dest=31` in `mission.py`; see `missions/README.md`'s note
on that heuristic before copying this pattern into another mission).

Type-105 (`suchai_4_beacon`) status frames from the OBC (node=1) decode
into 39 housekeeping parameters — see `telemetry.py`. The field layout,
types and node/tm_type enums come from SPEL/Universidad de Chile's own
`suchai4.ksy` (gitlab.com/spel-uchile/satnogs-decoders, branch
adding-suchai-4) — a first-party source from the team that operates the
satellite, not reverse-engineered — and are verified against six real
frames across the two independently-captured sessions: command/fail
counters increase monotonically at exactly the decoded beacon period
between frames, and `dat_com_freq`/`dat_com_baud` match our own RX
configuration exactly. Two fields don't decode at face value
(`dat_obc_last_reset`, `dat_eps_temp_bat0`) and the onboard clock reads
weeks behind wall-clock time — see `telemetry.py`'s module docstring.

Any other frame type still logs raw with CSP header facts only — see
`ax100_rx.Ax100RxPacketOps`.

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
- The telecommand ICD itself — command IDs, argument encoding/byte
  layout, any checksum/CRC scheme on the command body. `suchai4.ksy`
  only decodes downlink telemetry beacons (every `tm_type` value is a
  telemetry/file/message/beacon kind); it defines no structure for a
  command frame, though `tm_type=104` (`suchai_4_message`) hints one
  exists in their on-air protocol. Do not attempt to reverse-engineer or
  guess at command formats; sending malformed commands to someone else's
  spacecraft is a real risk, not just a bug.
- The CSP destination **port** commands should target. `suchai4.ksy`'s
  `nodes` enum does give real CSP node IDs (e.g. `dest=1` for
  `suchai_4_obc`) — see "Next steps" below — but the beacon's `dport=20`
  is the port telemetry is sent *from*, not confirmed to be the port a
  command-receiving service listens *on*.
- Whether `dat_obc_last_reset` and `dat_eps_temp_bat0` mean what their
  `.ksy` doc strings say (see `telemetry.py`).
- Whether the ~44 trailing bytes past the decoded 164-byte status struct
  (frame padding? a CSP-level CRC we haven't verified?) are worth
  decoding, or genuinely are just frame padding.

## Tracking: suchai4.ksy upstream merge

`suchai4.ksy` isn't merged into upstream `satnogs-decoders` yet. There's
an open MR for it, checked 2026-09-20:

  gitlab.com/librespacefoundation/satnogs/satnogs-decoders/-/merge_requests/529
  "Add SUCHAI-4" — author carlgonz (Carlos González), opened 2026-07-08,
  source spel-uchile/satnogs-decoders:adding-suchai-4 -> master.
  State: open, no merge conflicts, blocked on unresolved discussion
  (2 review notes as of the check date, content not pulled).

Re-check this MR if the decode ever stops matching real captures —
`telemetry.py` was ported from the branch tip at the time of this work,
and a rebase/force-push or merge-time changes during review could drift
from what's implemented here. (An earlier MR, !528, same title, was
opened and closed same-day 2026-07-08 from a differently-named branch
`add-suchai-4` — superseded by !529, not a live alternative.)

## Next steps

This organization is pursuing direct collaboration with the Universidad
de Chile SUCHAI team to obtain a real ICD for uplink command support
(telemetry decoding is now covered — see above).

### To complete the TX (uplink) chain, need from the Chilean team:

- **Telecommand ICD** — command IDs, argument encoding/byte layout, any
  checksum/CRC scheme on the command body. Nothing in `suchai4.ksy`
  covers this; it's strictly a downlink telemetry decoder.
- **Command destination port** — `suchai4.ksy`'s `nodes` enum already
  gives us the CSP node map, so the destination *node* is largely known:
  `dest=1` obc, `2` eps, `3` payload_bio, `4` payload_adcs, `5` trx, `6`
  payload_phy, `7` payload_sdr, `29` tnc, `30` ground_station, `31` test.
  Still need the destination *port* a command service listens on — the
  beacon's `dport=20` is a downlink service port, not confirmed to be
  the uplink command port.
- **Uplink RF parameters** — baud/deviation/coding for the uplink, which
  may differ from the downlink already captured. Nothing in
  `suchai4.ksy` covers this either — it operates on already-demodulated
  bytes, not RF/PHY parameters.
- **Command verification protocol** — likely already covered by fields
  we already decode: `dat_com_count_tc` / `dat_com_last_tc` (telecommand
  count and last-received timestamp) and `dat_obc_executed_cmds` /
  `dat_obc_failed_cmds`. Once a real command is sent, these should show
  receipt/execution — still need the Chilean team to confirm that's the
  intended mechanism rather than assuming it from field names alone.

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
