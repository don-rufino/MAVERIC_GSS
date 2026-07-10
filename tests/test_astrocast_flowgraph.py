"""Focused guards for the MAV-style Astrocast GNU Radio flowgraph."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
import re
import sys

import numpy as np
import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gnuradio"))


def _flowgraph_module():
    pytest.importorskip("gnuradio")
    import MAV_ASTROCAST

    return MAV_ASTROCAST


def test_live_frontend_matches_maveric_rx_conventions():
    flowgraph = _flowgraph_module()

    assert flowgraph.SAMP_RATE == 1_000_000
    assert flowgraph.RX_DECIM == 5
    assert flowgraph.ACQUISITION_RATE == 200_000
    assert flowgraph.RX_GAIN == 40
    assert flowgraph.DEFAULT_RX_LO_OFFSET_HZ == 250_000


def test_live_frontend_taps_exactly_match_maveric():
    flowgraph = _flowgraph_module()
    source = (ROOT / "gnuradio" / "MAV_DUO.py").read_text(encoding="utf-8")
    match = re.search(
        r"self\.fir_filter_xxx_1 = filter\.fir_filter_ccc\(rx_decim, "
        r"(\[.*?\])\)",
        source,
        re.DOTALL,
    )
    assert match is not None
    expected = np.asarray(ast.literal_eval(match.group(1)))
    actual = np.asarray(flowgraph._rx_frontend_taps())

    np.testing.assert_array_equal(actual, expected)


def test_live_frontend_forces_maveric_idle_rx_gpio_state():
    flowgraph = _flowgraph_module()

    class FakeUsrp:
        def __init__(self):
            self.calls = []

        def set_gpio_attr(self, bank, attr, value, mask):
            self.calls.append((bank, attr, value, mask))

    usrp = FakeUsrp()
    flowgraph._force_rx_relay(usrp)

    mask = 0b1111
    assert usrp.calls == [
        ("FP0", "CTRL", 0b0000, mask),
        ("FP0", "OUT", 0b1110, mask),
        ("FP0", "DDR", mask, mask),
    ]


def test_live_path_is_one_native_satellite_decoder_without_custom_acquisition():
    flowgraph = _flowgraph_module()
    source = inspect.getsource(flowgraph._build_core)

    assert "samp_rate=ACQUISITION_RATE, iq=True" in source
    assert "fir_filter_ccc" in source
    assert "freq_xlating" not in source
    assert "quadrature_demod" not in source
    assert not hasattr(flowgraph, "_BeaconAfcSink")
    assert not hasattr(flowgraph, "BEACON_BIN_CENTERS_HZ")


def test_decoder_yaml_is_native_astrocast_1k2_subset():
    pytest.importorskip("gnuradio")
    from satellites.satyaml import yamlfiles

    native = yamlfiles.open_satyaml(name="Astrocast 0.1")
    ours = yaml.safe_load(
        (ROOT / "gnuradio" / "ASTROCAST_DECODER.yml").read_text(
            encoding="utf-8"
        )
    )
    beacon_names = {
        "1k2 FSK FX.25 NRZ-I downlink",
        "1k2 FSK FX.25 NRZ downlink",
    }

    assert set(ours["transmitters"]) == beacon_names
    assert ours["transmitters"] == {
        name: native["transmitters"][name] for name in beacon_names
    }
