"""Focused guards for the MAV-style Astrocast GNU Radio flowgraph."""

from __future__ import annotations

import ast
import inspect
import os
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


def test_live_path_has_translated_frequency_search_branches():
    flowgraph = _flowgraph_module()
    source = inspect.getsource(flowgraph._build_core)

    assert "samp_rate=BEACON_DECODER_RATE, iq=False" in source
    assert "fir_filter_ccc" in source
    assert "freq_xlating_fir_filter_ccf" in source
    assert "quadrature_demod_cf" in source
    assert not hasattr(flowgraph, "_BeaconAfcSink")

    assert flowgraph.BEACON_DECODER_RATE == 20_000
    assert flowgraph.BEACON_BRANCH_CENTERS_HZ == (
        -8_000.0,
        0.0,
        8_000.0,
    )
    assert source.count("satellites.core.gr_satellites_flowgraph(") == 2

    # The closest branch is never more than 4 kHz away over the requested
    # +/-12 kHz acquisition range. Its filter passes both +/-1.2 kHz tones.
    for residual_hz in range(-12_000, 12_001, 1_000):
        nearest_hz = min(
            abs(residual_hz - center_hz)
            for center_hz in flowgraph.BEACON_BRANCH_CENTERS_HZ
        )
        assert nearest_hz <= 4_000.0
        assert (
            nearest_hz + flowgraph.BEACON_DEVIATION_HZ
            <= flowgraph.BEACON_CHANNEL_CUTOFF_HZ
        )


def test_live_search_bank_covers_satnogs_observed_frequency():
    flowgraph = _flowgraph_module()

    satnogs_drift_hz = -8_006.0
    nearest_hz = min(
        abs(satnogs_drift_hz - center_hz)
        for center_hz in flowgraph.BEACON_BRANCH_CENTERS_HZ
    )
    assert (
        nearest_hz + flowgraph.BEACON_DEVIATION_HZ
        <= flowgraph.BEACON_CHANNEL_CUTOFF_HZ
    )


def test_search_branch_pdu_deduplicator_uses_payload_and_holdoff():
    flowgraph = _flowgraph_module()
    pmt = pytest.importorskip("pmt")
    deduplicator = flowgraph._PduDeduplicator(holdoff_s=2.0)
    first = pmt.cons(
        pmt.make_dict(),
        pmt.init_u8vector(4, [0x01, 0x02, 0x03, 0x04]),
    )
    same_payload_other_metadata = pmt.cons(
        pmt.dict_add(pmt.make_dict(), pmt.intern("branch"), pmt.from_long(1)),
        pmt.init_u8vector(4, [0x01, 0x02, 0x03, 0x04]),
    )
    different = pmt.cons(
        pmt.make_dict(),
        pmt.init_u8vector(4, [0x01, 0x02, 0x03, 0x05]),
    )

    assert not deduplicator._is_duplicate(first, now=10.0)
    assert deduplicator._is_duplicate(same_payload_other_metadata, now=10.1)
    assert not deduplicator._is_duplicate(different, now=10.2)
    assert not deduplicator._is_duplicate(first, now=12.2)


def test_astrocast_waterfall_logger_writes_renderer_record_format(
        monkeypatch, tmp_path):
    flowgraph = _flowgraph_module()
    import waterfall_render

    monkeypatch.setenv("GSS_MISSION", "astrocast")
    monkeypatch.setenv("GSS_RX_FREQ_HZ", "437150000")
    monkeypatch.setenv("GSS_WATERFALL_DIR", os.fspath(tmp_path))
    logger = flowgraph._WaterfallLogger()
    phase = np.linspace(
        0.0,
        10.0 * np.pi,
        logger.FFT_SIZE * logger.FFTS_PER_ROW,
        dtype=np.float32,
    )
    samples = np.exp(1j * phase).astype(np.complex64)

    assert logger.work([samples], []) == len(samples)
    dat_path = Path(logger._dat_path)
    assert dat_path.name.startswith("waterfall_astrocast_")
    assert dat_path.stat().st_size == waterfall_render.ROW_BYTES
    timestamps, rows = waterfall_render.load_rows(dat_path)
    assert timestamps.shape == (1,)
    assert rows.shape == (1, waterfall_render.FFT_BINS)

    assert logger.stop()
    assert not dat_path.exists()
    assert dat_path.with_suffix(".png").is_file()


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
