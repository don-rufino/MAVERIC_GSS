# NUSHSat-1 mission package

RX-only mission for NUSHSat-1 (NUS High School of Math and Science,
Singapore, NORAD 63211, 436.200 MHz). The operational beacon is AX100
Mode 5 (ASM+Golay) 1k2 FSK at 575 Hz deviation, selected by the dedicated
`gnuradio/decoders/NUSHSAT1_DECODER.yml` profile. SatNOGS also marks 2k4 active;
because its deviation is not pinned, the locator keeps 800 Hz (AX100 auto
h=2/3) and 600 Hz (h=0.5) branches. The dormant 4k8 mode is excluded.

gr-satellites classifies the telemetry as bare CSP (`CSP telemetry:
csp`) with no public payload format — packets log raw with CSP header
facts. SatNOGS flags the transmitters as IARU-uncoordinated.
