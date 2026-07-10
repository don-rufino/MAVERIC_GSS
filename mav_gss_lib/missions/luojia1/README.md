# LUOJIA-1 mission package

RX-only mission for LUOJIA-1 (Wuhan University, NORAD 43485,
437.250 MHz). AX100 Mode 5 (ASM+Golay) 4k8 FSK CSP downlink with 1600 Hz
deviation, decoded by the stock MAVERIC flowgraph's
`4k8 FSK AX100 ASM+Golay downlink` branch.

gr-satellites classifies the telemetry as bare CSP (`telemetry: csp`) with
no public payload format — packets log raw with CSP header facts.
