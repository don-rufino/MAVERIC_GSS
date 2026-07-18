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


def test_live_frontend_uses_rf_b_rx2():
    source = (ROOT / "gnuradio" / "MAV_ASTROCAST.py").read_text(
        encoding="utf-8")

    assert "set_subdev_spec('A:B', 0)" in source
    assert 'set_antenna("RX2", 0)' in source


def test_live_frontend_matches_maveric_rx_conventions():
    flowgraph = _flowgraph_module()

    assert flowgraph.SAMP_RATE == 1_000_000
    assert flowgraph.RX_DECIM == 5
    assert flowgraph.ACQUISITION_RATE == 200_000
    assert flowgraph.RX_GAIN == 40
    assert flowgraph.DEFAULT_RX_LO_OFFSET_HZ == 250_000


def test_live_frontend_taps_exactly_match_maveric():
    flowgraph = _flowgraph_module()
    from gnuradio.filter import firdes

    source = (ROOT / "gnuradio" / "MAV_DUO.py").read_text(encoding="utf-8")
    match = re.search(
        r"self\.fir_filter_xxx_1 = filter\.fir_filter_ccf\(rx_decim, "
        r"firdes\.low_pass\(([^)]*)\)\)",
        source,
    )
    assert match is not None
    args = ast.literal_eval(
        "(%s,)" % match.group(1).replace("samp_rate", str(flowgraph.SAMP_RATE))
    )
    expected = np.asarray(firdes.low_pass(*args))
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
    banks_source = inspect.getsource(flowgraph._attach_decode_banks)
    core_source = inspect.getsource(flowgraph._build_core)

    assert "samp_rate=BEACON_DECODER_RATE, iq=False" in banks_source
    assert "freq_xlating_fir_filter_ccf" in banks_source
    assert "quadrature_demod_cf" in banks_source
    assert "fir_filter_ccf" in core_source
    assert not hasattr(flowgraph, "_BeaconAfcSink")

    assert flowgraph.BEACON_DECODER_RATE == 20_000
    assert flowgraph.BEACON_BRANCH_CENTERS_HZ == tuple(
        float(hz) for hz in range(-12_000, 12_001, 2_000)
    )
    # One decoder instantiation in the shared banks, one in the wav path.
    assert banks_source.count("satellites.core.gr_satellites_flowgraph(") == 1
    assert core_source.count("satellites.core.gr_satellites_flowgraph(") == 1

    # The live USRP path and the --iqfile replay path must feed the SAME
    # bank constructor — replay through anything less than the production
    # chain is not a valid regression signal.
    assert core_source.count("_attach_decode_banks(") == 2
    assert "_attach_decode_banks(tb, tb.rx_lpf)" in core_source
    assert "_attach_decode_banks(tb, tb.blocks_iq_throttle)" in core_source

    # The closest branch is never more than 1 kHz away over the requested
    # +/-12 kHz acquisition range, and both +/-1.2 kHz tones stay inside the
    # flat passband (below cutoff - transition/2), so every branch presents
    # a matched pre-detection bandwidth to the discriminator.
    passband_edge_hz = (
        flowgraph.BEACON_CHANNEL_CUTOFF_HZ
        - flowgraph.BEACON_CHANNEL_TRANSITION_HZ / 2
    )
    for residual_hz in range(-12_000, 12_001, 500):
        nearest_hz = min(
            abs(residual_hz - center_hz)
            for center_hz in flowgraph.BEACON_BRANCH_CENTERS_HZ
        )
        assert nearest_hz <= 1_000.0
        assert nearest_hz + flowgraph.BEACON_DEVIATION_HZ <= passband_edge_hz


def test_live_matched_filter_bank_geometry_and_wiring():
    flowgraph = _flowgraph_module()

    centers = flowgraph.MATCHED_FILTER_BRANCH_CENTERS_HZ
    assert centers == tuple(float(hz) for hz in range(-3_000, 3_001, 500))

    # Tone correlators lose ~0.6 dB at 250 Hz of mistune; every residual in
    # the fine-bank span must land within that of a centre.
    for residual_hz in range(-3_000, 3_001, 50):
        nearest_hz = min(abs(residual_hz - c) for c in centers)
        assert nearest_hz <= 250.0

    bank_source = inspect.getsource(flowgraph._attach_matched_filter_bank)
    assert "astrocast_fx25_deframer" in bank_source
    assert "complex_to_mag" in bank_source
    assert "quadrature_demod" not in bank_source  # no discriminator here

    banks_source = inspect.getsource(flowgraph._attach_decode_banks)
    assert "_attach_matched_filter_bank" in banks_source
    assert "matched_filter_deframers" in banks_source


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
        - flowgraph.BEACON_CHANNEL_TRANSITION_HZ / 2
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
        (ROOT / "gnuradio" / "decoders" / "ASTROCAST_DECODER.yml").read_text(
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


def test_stream_instrumentation_in_live_path():
    flowgraph = _flowgraph_module()
    core_source = inspect.getsource(flowgraph._build_core)
    # Health probe + raw diagnostic recorder tap the raw USRP stream, before
    # the front FIR — replay modes carry neither (no USRP, no overflows).
    assert "(tb.uhd_usrp_source_0, 0), (tb.stream_health, 0)" in core_source
    assert "(tb.uhd_usrp_source_0, 0), (tb.iq_raw_recorder, 0)" in core_source
    assert "GSS_IQ_RAW_RECORD" in core_source
    assert "maveric:rx_gain_db" in inspect.getsource(flowgraph._IqRecorder)
    assert flowgraph.RX_GAIN == 40.0  # env-driven boot value, default 40


def test_stream_health_report_matches_radio_service_contract():
    import json as _json

    flowgraph = _flowgraph_module()
    mon = flowgraph._StreamHealthMonitor(samp_rate=1e6)
    mon._sumsq = (0.5 ** 2) * 1000
    mon._samples = 1000
    mon._peak = 0.9
    mon._clip = 3
    mon._overflows = 2
    line = mon._render_report(10.0)
    assert line.startswith("STREAM_HEALTH ")
    payload = _json.loads(line[len("STREAM_HEALTH "):])
    assert payload["rms_dbfs"] == -6.0
    assert payload["peak_dbfs"] == -0.9
    assert payload["clip_count"] == 3
    assert payload["overflows_total"] == 2
    assert payload["span_s"] == 10.0


def test_replay_frame_bus_defaults_to_throwaway_endpoint():
    flowgraph = _flowgraph_module()
    # Live keeps the production bus; replays must never default onto it —
    # a historical recording replayed with the backend up would be ingested
    # as CURRENT telemetry (and the PUB bind collides with a live flowgraph).
    assert flowgraph._resolve_zmq_addr(None, False) == flowgraph.FRAME_ZMQ_ADDR
    addr = flowgraph._resolve_zmq_addr(None, True)
    assert addr.startswith("ipc://")
    assert "52001" not in addr
    # macOS caps unix-socket paths at 104 chars
    assert len(addr) - len("ipc://") < 104
    # explicit override (including onto the GSS bus) is honoured verbatim
    assert flowgraph._resolve_zmq_addr("tcp://127.0.0.1:52001", True) == \
        "tcp://127.0.0.1:52001"


def test_iq_recorder_stamps_sample_zero_from_uhd_rx_time():
    flowgraph = _flowgraph_module()
    source = inspect.getsource(flowgraph._IqRecorder)
    # Construction time is retained only as fallback/provenance. Sample zero
    # is derived from the fractional UHD rx_time tag and its sample offset.
    assert "maveric:constructed_utc" in source
    assert "_rx_time_seconds" in source
    assert "zero_seconds = seconds - sample_start / self._samp_rate" in source
    assert 'timespec="microseconds"' in source
