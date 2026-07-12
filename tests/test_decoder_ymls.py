"""Guards for the per-mission gr-satellites decoder databases.

MAV_DUO selects <GSS_MISSION>_DECODER.yml when it ships next to the
flowgraph (GSS_DECODER_YML overrides; MAVERIC_DECODER.yml is the
fallback). These tests pin each database's measured/official modulation
parameters so a stray edit cannot silently cost decode margin.
"""

from __future__ import annotations

from pathlib import Path

import yaml

GNURADIO = Path(__file__).resolve().parent.parent / "gnuradio"

_KNOWN_FRAMINGS = {"AX100 ASM+Golay", "AX100 Reed Solomon", "AX.25 G3RUH", "AX.25",
                   "Astrocast FX.25 NRZ-I", "Astrocast FX.25 NRZ"}


def _load(name):
    doc = yaml.safe_load((GNURADIO / name).read_text(encoding="utf-8"))
    assert doc["transmitters"], name
    for key, tx in doc["transmitters"].items():
        assert tx["framing"] in _KNOWN_FRAMINGS, (name, key, tx["framing"])
        assert tx["baudrate"] > 0 and tx["deviation"] > 0, (name, key)
    return doc


def _branch(doc, baud, framing):
    return [tx for tx in doc["transmitters"].values()
            if tx["baudrate"] == baud and tx["framing"] == framing]


def test_roads_decoder_pins_measured_h05_deviations():
    doc = _load("ROADS_DECODER.yml")
    m5_4k8 = _branch(doc, 4800, "AX100 ASM+Golay")
    m5_9k6 = _branch(doc, 9600, "AX100 ASM+Golay")
    # 4k8 dev 1200 measured from the first decoded ROADS 2 frame (h = 0.5);
    # 9k6 dev 2400 carries that measured h across bauds (modindex is one
    # radio-wide param; the AX100 auto default would be 2/3 — ROADS sets 0.5).
    assert [tx["deviation"] for tx in m5_4k8] == [1200]
    assert [tx["deviation"] for tx in m5_9k6] == [2400]


def test_sharjahsat_decoder_is_single_official_branch():
    doc = _load("SHARJAHSAT_DECODER.yml")
    assert len(doc["transmitters"]) == 1
    (tx,) = doc["transmitters"].values()
    assert tx["baudrate"] == 9600
    assert tx["framing"] == "AX.25 G3RUH"
    assert tx["deviation"] == 3000  # official gr-satellites Sharjahsat-1.yml


def test_maveric_decoder_keeps_dual_hypothesis_branches():
    doc = _load("MAVERIC_DECODER.yml")
    m5_9k6 = _branch(doc, 9600, "AX100 ASM+Golay")
    m5_4k8 = _branch(doc, 4800, "AX100 ASM+Golay")
    # Flight param dump shows modindex 0.0 = AUTOMATIC (h = 2/3 for
    # 1300-60000 baud per the manual): dev 3200 / 1600 are the primary
    # hypotheses, with h=0.5 secondaries (2400 / 1200) alongside until
    # MAVERIC's first frame settles it. 4k8 dev 1600 is also the official
    # value for the fallback birds Luojia-1 / AISTECHSAT-2.
    assert sorted(tx["deviation"] for tx in m5_9k6) == [2400, 3200]
    assert sorted(tx["deviation"] for tx in m5_4k8) == [1200, 1600]
    # CATSAT's 2k4 branch (official dev 750) still rides the fallback file.
    m5_2k4 = _branch(doc, 2400, "AX100 ASM+Golay")
    assert [tx["deviation"] for tx in m5_2k4] == [750]
