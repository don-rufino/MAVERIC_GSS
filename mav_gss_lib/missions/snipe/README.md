# SNIPE mission package

RX-only mission for the KASI SNIPE formation (2023-072) — four 6U CubeSats
sharing one AX100 Mode 5 (ASM+Golay) 4k8 FSK CSP downlink format on four
different frequencies. Decoded by MAV_DUO using the dedicated
`gnuradio/decoders/SNIPE_DECODER.yml` family profile. The published
definitions do not pin modulation index, so only the 1600 and 1200 Hz
4k8 hypotheses run until real IQ settles it. The CSP housekeeping payload
is not publicly documented, so packets log raw with CSP header facts.

One mission covers the formation. The seeded default target is **SNIPE-1**
(cataloged as 2023-072G). To work another member, change two operator
values and restart the radio:

| Spacecraft | NORAD | Frequency |
|---|---|---|
| SNIPE-1 (default) | 56749 | 435.450 MHz |
| SNIPE-2 | 56745 | 436.000 MHz |
| SNIPE-3 | 56746 | 436.950 MHz |
| SNIPE-4 | 56744 | 437.800 MHz |

Radio·RF pane → RX frequency; Tracking pane → TLE identifier (fetch), then
re-engage Doppler.
