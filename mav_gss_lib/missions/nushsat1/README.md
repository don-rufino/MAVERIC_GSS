# NUSHSat-1 mission package

RX-only mission for NUSHSat-1 (NUS High School of Math and Science,
Singapore, NORAD 63211, 436.200 MHz). AX100 Mode 5 (ASM+Golay) CSP
downlink at 1k2 / 2k4 / 4k8 FSK, decoded by the stock MAVERIC flowgraph's
`2k4 FSK AX100 ASM+Golay downlink` and `4k8 FSK AX100 ASM+Golay downlink`
branches (the 1k2 variant is below the decoder's branch set; add a 1k2
branch to `gnuradio/MAVERIC_DECODER.yml` if it turns out to be the
dominant beacon rate).

gr-satellites classifies the telemetry as bare CSP (`CSP telemetry:
csp`) with no public payload format — packets log raw with CSP header
facts. SatNOGS flags the transmitters as IARU-uncoordinated.
