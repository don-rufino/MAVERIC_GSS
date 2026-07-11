# ROADS mission package

RX-only mission for the University of North Dakota ROADS pair (2025-135) —
two CubeSats sharing one AX100 Mode 5 (ASM+Golay) CSP downlink format on a
single frequency, 435.400 MHz, at 4k8 and 9k6 FSK. Decoded by the stock
MAVERIC flowgraph's `4k8 FSK AX100 ASM+Golay downlink` and
`9k6 FSK AX100 ASM+Golay downlink` branches.

Housekeeping beacons decode per UND's published "IARU Telemetry Decoding
Format" (`telemetry.py` ports the doc from
`aero.und.edu/space/operations-group/roads-mission.html`): a 5-byte header
(protocol_version 1, type, version, satid) plus 42 GomSpace-style elements
(checksum/timestamp/source wrapper + big-endian value) across obc / gnss /
eps / uhf / vhf domains — 47 parameters in total.

The documented full table is 444 bytes, which cannot ride one AX100
Mode 5 frame (the data field is a single RS(255,223) codeword, capping
the inner CSP packet at 223 bytes), so on air it must arrive as smaller
typed beacons. The decoder matches a frame by exact length against every
contiguous run of whole subsystem blocks that fits the cap — obc (104),
gnss (41), eps (173), uhf/vhf (73, length-ambiguous, assumed uhf with a
warning), obc+gnss (140), gnss+eps (209), uhf+vhf (141) — plus the full
table for offline reassembled use. Frames that match no run (wrong
protocol version, unknown lengths, non-beacon traffic) log raw with CSP
header facts, so nothing is lost while the real on-air slicing is still
unconfirmed. Decoded telemetry reports can be submitted to UND at
`undsog@space.edu`.

One mission covers the pair. The seeded default target is **ROADS 1**
(cataloged as 2025-135H). Because both spacecraft share the downlink
frequency, working ROADS 2 is a tracking-only change:

| Spacecraft | NORAD | Frequency |
|---|---|---|
| ROADS 1 (default) | 64535 | 435.400 MHz |
| ROADS 2 | 64549 | 435.400 MHz |

Tracking pane → TLE identifier (fetch 64549), then re-engage Doppler. The
RX frequency stays put.
