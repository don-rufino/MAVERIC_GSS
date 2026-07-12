# Suomi 100 mission package

RX-only mission for Suomi 100 (Finland's centennial CubeSat, Aalto
University / University of Helsinki, NORAD 43804, 437.775 MHz). AX100
Mode 5 (ASM+Golay) 9k6 FSK CSP downlink with 2400 Hz deviation, decoded by
the dedicated `gnuradio/decoders/SUOMI100_DECODER.yml` profile.

Unlike the other AX100-family packages, Suomi 100's housekeeping format is
public: two beacon types (selected by the first payload byte) carrying
GomSpace NanoMind-family EPS / COM / OBC blocks. `telemetry.py` ports the
gr-satellites `suomi100` construct layout and emits 75 parameters across
`eps` / `com` / `obc` domains — battery and photovoltaic voltages/currents
(P31u raw mV/mA), temperatures, radio RSSI and frequency error, watchdogs,
boot counters, and lifetime traffic counters. Unrecognized payloads log
raw as opaque telemetry.

On-air frames carry two nested CRC-32C words after the beacon block. The
mission decoder verifies both before presenting a clean packet.
