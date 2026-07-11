# AISTECHSAT-2 mission package

RX-only mission for AISTECHSAT-2 (Aistech Space, NORAD 43768,
436.600 MHz). AX100 Mode 5 (ASM+Golay) CSP downlink at 4k8 FSK
(deviation 1600 Hz) and 9k6 FSK, decoded by the stock MAVERIC flowgraph's
`4k8 FSK AX100 ASM+Golay downlink` and `9k6 FSK AX100 ASM+Golay downlink`
branches.

Standard housekeeping frames follow the ECSS PUS telemetry stack that
gr-satellites decodes with its `lume` parser (shared by LUME-1 and
AISTECHSAT-2; DK3WN's 2019 joint decoder corroborates the shared format).
`telemetry.py` ports it: after the CSP header come a CCSDS TM
transfer-frame header, Space Packet primary header, PUS TM secondary
header, and a day/ms time field; a u16 payload id selects one of five
housekeeping tables:

| id | container | content | tokens |
|---|---|---|---|
| 1 | `obc` | boot/clock/flash/mag/gyro | 31 |
| 2 | `eps` | GomSpace P31u-style power HK | 60 |
| 3 | `ttc` | GSSB antenna + AX100-style radio HK | 36 |
| 4 | `aocs` | attitude sensors, GPS, currents | 33 |
| 5 | `temps` | temperature spreads | 27 |

`mission.yml` is generated from the field table in `telemetry.py`
(guarded by `test_aistechsat2_yml_containers_match_field_table`). Unknown
payload ids — including Aistech's undocumented "custom telemetry" — log
raw with CSP header facts.
