# CATSAT mission package

RX-only mission for CATSAT (University of Arizona 6U, NORAD 60246,
437.185 MHz). AX100 Mode 5 (ASM+Golay) CSP downlink: routine beacons at
2k4 FSK with 750 Hz deviation, data bursts at 9k6 / 38k4. The MAVERIC
decoder's `2k4 FSK AX100 ASM+Golay downlink` branch exists specifically to
catch the CATSAT beacon; 9k6 and 38k4 ride the standard Mode 5 branches.

All 18 beacon types decode into parameters: `telemetry.py` ports the
community Kaitai definition (satnogs-decoders `catsat.ksy`) as a
machine-generated field table spanning the whole GomSpace stack — MOTD
(callsign + message of the day), CRIT1/CRIT2 vitals (P60/BPX battery, OBC,
AX100 board temp), the OBC beacon's radio self-report (`ax100_rx/tx_freq`,
`last_rssi`, `rferr`, `bgnd_rssi`, `tx_duty`), PDU/ACU power, deployment
status, seven ADCS beacons (magnetometer, wheels, UKF state, ephemeris),
and the ASDR payload. 621 parameters across sys/crit/obc/pwr/dep/adcs/asdr
domains; unrecognized types log raw as opaque telemetry.

Wire quirk: CATSAT transmits its CSP header **little-endian** (un-swapped
libcsp), so the mission constructs the shared ops with
`csp_endianness="little"`. The ksy's `destination` bit expression carries
a shift typo; the port uses the standard CSP v1 bitfield read as a
little-endian uint32, consistent with every other field in the ksy.
