# ROADS mission package

RX-only mission for the University of North Dakota ROADS pair (2025-135) —
two CubeSats sharing one AX100 Mode 5 (ASM+Golay) CSP downlink format on a
single frequency, 435.400 MHz, at 4k8 and 9k6 FSK. Decoded by the stock
MAVERIC flowgraph's `4k8 FSK AX100 ASM+Golay downlink` and
`9k6 FSK AX100 ASM+Golay downlink` branches.

Housekeeping beacons decode per the **on-air type-0x66 full-table format**,
reverse-engineered from the first frame received (ROADS 2, 2026-07-11
21:36:02Z, 4k8 FSK AX100 ASM+Golay, CSP CRC-32C verified — the golden
fixture in `tests/test_missions_ax100_rx.py`). UND's published "IARU
Telemetry Decoding Format" (`aero.und.edu`) describes the same 42-field
table across obc / gnss / eps / uhf / vhf domains but accounts an 8-byte
checksum/timestamp/source wrapper per *element* (444 bytes total); on the
wire the wrapper amortizes per sample *group*, so the whole table rides
one 156-byte Mode 5 frame:

    5-byte header (protocol_version 1, type 0x66, version, satid u16)
    six wrapped groups — obc main (15B), obc deploy (4B), gnss (12B),
    eps (32B), uhf (18B), vhf (18B) — each led by checksum u16 +
    unix timestamp u32 + source node u16, values big-endian
    CSP CRC-32C trailer (present when the CSP header sets flags bit 0)

The radio groups are 18 bytes, not the document's 20: the tail decodes as
boot_count u16 + boot_cause u32 (hex-rendered). One timestamp token per
domain plus 42 values = 47 parameters into the `beacon_hk` container.
Frames with any other beacon type, a wrong protocol version, or a failing
CRC log raw with CSP header facts, so nothing is lost as further beacon
types show up. Decoded telemetry reports can be submitted to UND at
`undsog@space.edu`.

Operational note from the first-decode pass: the two spacecraft fly in
formation on the same frequency, and the *untracked* bird appears a few
kHz off the engaged Doppler solution (observed +2.3 to +5.8 kHz across a
pass) — outside the 4k8 branch's Carson tolerance, so it will not decode.
Point the Doppler at the spacecraft you want frames from.

One mission covers the pair. The seeded default target is **ROADS 1**
(cataloged as 2025-135H). Because both spacecraft share the downlink
frequency, working ROADS 2 is a tracking-only change:

| Spacecraft | NORAD | Frequency |
|---|---|---|
| ROADS 1 (default) | 64535 | 435.400 MHz |
| ROADS 2 | 64549 | 435.400 MHz |

Tracking pane → TLE identifier (fetch 64549), then re-engage Doppler. The
RX frequency stays put.
