"""SUCHAI-4 beacon (type-105 `suchai_fs_status`) housekeeping decoder.

Field layout, types and the node/tm_type enums are taken verbatim from
`suchai4.ksy` (gitlab.com/spel-uchile/satnogs-decoders, branch
adding-suchai-4) — a first-party source published by SPEL, Universidad de
Chile, the team that operates SUCHAI-4. Not reverse-engineered: verified
against six real captured frames across two independent sessions
(2026-08-28 and 2026-09-11) — every field lines up with real-world
semantics (dat_com_freq matches our own configured RX frequency exactly;
dat_com_baud matches "4k8"; command/fail counters increase monotonically
between frames at exactly dat_com_bcn_period spacing; dest=30 resolves via
the ksy's own `nodes` enum to suchai_4_ground_station).

Two fields are decoded as raw integers rather than the "obvious" richer
type, because the obvious type doesn't hold up against real captures:

  - dat_obc_last_reset: the .ksy docs it "Last reset time (s)" like an
    absolute Unix timestamp, but it reads 4 on every frame across both
    sessions — a real epoch would show 1970-01-01T00:00:04Z. Left as a
    plain integer (seconds since boot? a counter? unclear) rather than
    rendered as a bogus ISO date.
  - dat_eps_temp_bat0: typed u4 in the .ksy (unlike dat_obc_temp_1, which
    is f4) despite the doc string calling it "EPS Temperature (degC)".
    No confirmed scaling factor, so it's shown as a raw integer, not
    assumed to be whole-degree Celsius.

timestamp / dat_rtc_date_time / dat_com_last_tc decode fine as absolute
Unix time and render as ISO-8601 UTC tokens, but the onboard clock itself
runs well behind wall-clock reality (~30 days on 2026-09-11, ~3 weeks on
2026-08-28) — self-consistent within a session (advances by exactly
dat_com_bcn_period between frames), so this looks like RTC drift/unsync
on the spacecraft, not a decode bug here.
"""

from __future__ import annotations

import struct
from datetime import datetime, timezone

from mav_gss_lib.missions.ax100_rx import HkDecode


_HEADER = struct.Struct(">HBBI")  # nframe, type, node, ndata

BEACON_TM_TYPE = 105  # suchai_4_beacon, per suchai4.ksy's tm_type enum
OBC_NODE = 1          # suchai_4_obc

NODES = {
    1: "suchai_4_obc", 2: "suchai_4_eps", 3: "suchai_4_payload_bio",
    4: "suchai_4_payload_adcs", 5: "suchai_4_trx", 6: "suchai_4_payload_phy",
    7: "suchai_4_payload_sdr", 29: "suchai_4_tnc", 30: "suchai_4_ground_station",
    31: "suchai_4_test",
}

# (domain, field key, value format, render) in wire order — mirrors
# suchai4.ksy's `status` type exactly.
_STATUS_FIELDS = (
    ("tm",  "index",                  "I", "int"),
    ("tm",  "timestamp",              "I", "iso"),
    ("obc", "dat_rtc_date_time",      "I", "iso"),
    ("obc", "dat_obc_last_reset",     "I", "int"),
    ("obc", "dat_obc_hrs_alive",      "I", "int"),
    ("obc", "dat_obc_hrs_wo_reset",   "I", "int"),
    ("obc", "dat_obc_reset_counter",  "I", "int"),
    ("obc", "dat_obc_executed_cmds",  "I", "int"),
    ("obc", "dat_obc_failed_cmds",    "I", "int"),
    ("com", "dat_com_count_tm",       "I", "int"),
    ("com", "dat_com_count_tc",       "I", "int"),
    ("com", "dat_com_last_tc",        "I", "iso"),
    ("fpl", "dat_fpl_last",           "I", "int"),
    ("fpl", "dat_fpl_queue",          "I", "int"),
    ("fpl", "dat_fpl_table",          "I", "int"),
    ("com", "dat_com_freq",           "I", "int"),
    ("com", "dat_com_tx_pwr",         "I", "int"),
    ("com", "dat_com_baud",           "I", "int"),
    ("com", "dat_com_bcn_period",     "I", "int"),
    ("eps", "dat_eps_vbatt",          "I", "int"),
    ("eps", "dat_eps_cur_sun",        "I", "int"),
    ("eps", "dat_eps_cur_sys",        "I", "int"),
    ("obc", "dat_obc_temp_1",         "f", "f1"),
    ("eps", "dat_eps_temp_bat0",      "I", "int"),
    ("sam", "dat_sam_mach_action",    "I", "int"),
    ("sam", "dat_sam_mach_state",     "I", "int"),
    ("sam", "dat_sam_mach_left",      "I", "int"),
    ("sam", "dat_sam_mach_step",      "I", "int"),
    ("sam", "dat_sam_mach_payloads",  "I", "int"),
    ("mm",  "dat_mm_mode",            "I", "int"),
    ("mm",  "dat_mm_sciencemode",     "I", "hex"),
    ("mm",  "dat_mm_vbatt_limit",     "I", "int"),
    ("mm",  "dat_mm_last_tc_limit",   "I", "int"),
    ("spl", "dat_spl_table_0",        "I", "int"),
    ("spl", "dat_spl_current_0",      "I", "int"),
    ("spl", "dat_spl_exec_0",         "I", "int"),
    ("spl", "dat_spl_recovery",       "I", "int"),
    ("ads", "dat_ads_tle_epoch",      "I", "int"),
    ("ads", "dat_ads_tle_last",       "I", "int"),
)

STATUS_SIZE = sum(struct.calcsize(">" + fmt) for _, _, fmt, _ in _STATUS_FIELDS)  # 156
BEACON_SIZE = _HEADER.size + STATUS_SIZE                                          # 164


# Fields where stripping the domain prefix would leave a too-generic id
# ("last", "queue", "table", "mode") — kept qualified instead. Must match
# mission.yml's parameter ids (guarded by
# test_suchai4_yml_containers_match_field_table).
_UNSTRIPPED = {
    "dat_fpl_last": "fpl_last", "dat_fpl_queue": "fpl_queue",
    "dat_fpl_table": "fpl_table", "dat_mm_mode": "mm_mode",
    "dat_spl_table_0": "spl_table_0", "dat_spl_current_0": "spl_current_0",
    "dat_spl_exec_0": "spl_exec_0", "dat_spl_recovery": "spl_recovery",
}


def token_names() -> tuple[str, ...]:
    """Container entry names in wire order, domain prefix stripped."""
    names = []
    for domain, key, _fmt, _render in _STATUS_FIELDS:
        if key in _UNSTRIPPED:
            names.append(_UNSTRIPPED[key])
            continue
        prefix = f"dat_{domain}_"
        stripped = key[len(prefix):] if key.startswith(prefix) else key.removeprefix("dat_")
        names.append(stripped)
    return tuple(names)


def _iso(unix_s: int) -> str:
    return datetime.fromtimestamp(unix_s, timezone.utc).isoformat(timespec="seconds")


def decode_beacon(csp_header: dict, payload: bytes) -> HkDecode | None:
    """Decode a SUCHAI-4 type-105 status beacon (bytes after the CSP header).

    Returns None when the frame is too short or isn't a status beacon from
    the OBC — the shared PacketOps then logs the frame raw with CSP header
    facts only, same as before this decoder existed.
    """
    if len(payload) < BEACON_SIZE:
        return None
    nframe, tm_type, node, ndata = _HEADER.unpack_from(payload, 0)
    if tm_type != BEACON_TM_TYPE or node != OBC_NODE:
        return None

    tokens: list[str] = []
    values: dict[str, int | float] = {}
    offset = _HEADER.size
    for _domain, key, fmt, render in _STATUS_FIELDS:
        (value,) = struct.unpack_from(">" + fmt, payload, offset)
        offset += struct.calcsize(">" + fmt)
        values[key] = value
        if render == "iso":
            tokens.append(_iso(value))
        elif render == "hex":
            tokens.append(f"0x{value & 0xFFFFFFFF:08x}")
        elif render == "f1":
            tokens.append(f"{value:.2f}")
        else:
            tokens.append(str(value))

    facts = {
        "kind": "hk",
        "tm_type": tm_type,
        "tm_type_name": "suchai_4_beacon",
        "node": node,
        "node_name": NODES.get(node, str(node)),
        "nframe": nframe,
        "ndata": ndata,
        "vbat_mv": values["dat_eps_vbatt"],
        "obc_temp_c": round(values["dat_obc_temp_1"], 2),
        "reset_counter": values["dat_obc_reset_counter"],
    }
    return HkDecode(
        container_kind="hk",
        tokens=" ".join(tokens).encode("ascii"),
        facts=facts,
    )
