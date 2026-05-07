import pytest

from mav_gss_lib.config import load_split_config
from mav_gss_lib.platform.tx.commands import CommandRejected
from mav_gss_lib.platform.runtime import PlatformRuntime


def test_runtime_v2_loads_echo_and_processes_rx_and_tx(tmp_path):
    runtime = PlatformRuntime.from_split(
        {"logs": {"dir": str(tmp_path)}},
        "echo_v2",
        {},
    )

    rx = runtime.process_rx({"transmitter": "fixture"}, b"\x01\x02")
    tx = runtime.prepare_tx("ping hello")

    assert runtime.mission.id == "echo_v2"
    assert rx.packet.raw.hex() == "0102"
    assert tx.encoded.raw == b"ping hello"


def test_runtime_v2_loads_balloon_silent_telemetry(tmp_path):
    """balloon_v2 has no declarative spec post-Task-4 — packets flow,
    parameters do not."""
    runtime = PlatformRuntime.from_split(
        {"logs": {"dir": str(tmp_path)}},
        "balloon_v2",
        {},
    )

    result = runtime.process_rx(
        {},
        b'{"type":"beacon","alt_m":1200,"lat":34.0,"lon":-118.2,"temp_c":18.4}',
    )

    assert runtime.mission.id == "balloon_v2"
    assert runtime.walker is None
    assert result.packet.parameters == ()


def test_runtime_v2_rejects_tx_for_non_commandable_mission(tmp_path):
    runtime = PlatformRuntime.from_split(
        {"logs": {"dir": str(tmp_path)}},
        "balloon_v2",
        {},
    )

    with pytest.raises(CommandRejected):
        runtime.prepare_tx("anything")


def test_runtime_v2_loads_maveric_with_walker_and_cache(tmp_path):
    platform_cfg, mission_id, mission_cfg = load_split_config()
    platform_cfg["logs"] = {"dir": str(tmp_path)}

    runtime = PlatformRuntime.from_split(platform_cfg, mission_id, mission_cfg)

    rx = runtime.process_rx({"transmitter": "fixture"}, b"\x01\x02")
    tx = runtime.prepare_tx("ftdi_log hello")

    assert runtime.mission.id == "maveric"
    assert runtime.walker is not None
    assert runtime.parameter_cache is not None
    assert rx.packet.flags.is_unknown is True
    assert tx.encoded.raw
    assert tx.encoded.cmd_id == "ftdi_log"
    # cmd_id is canonical at EncodedCommand.cmd_id; not duplicated under
    # mission_facts["header"] (post-Task-6).
    assert "cmd_id" not in tx.encoded.mission_facts["header"]
