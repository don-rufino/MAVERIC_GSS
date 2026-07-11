# InnoCube mission package

RX-only mission for InnoCube (TU Berlin / Uni Würzburg 3U+, NORAD 62616,
435.950 MHz, launched 2025-01-14). AX100 Mode 5 (ASM+Golay) 9k6 FSK CSP
downlink, decoded by the stock MAVERIC flowgraph's
`9k6 FSK AX100 ASM+Golay downlink` branch.

gr-satellites classifies the telemetry as bare CSP (`telemetry: csp`) with
no public payload format — packets log raw with CSP header facts. The
spacecraft also carries a CW beacon and SSTV experiment on 437.020 MHz
(outside this mission's scope).
