"""Guards for production, mission-specific gr-satellites databases."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
import yaml

from mav_gss_lib.platform.loader import load_mission_spec_from_split


ROOT = Path(__file__).resolve().parent.parent
GNURADIO = ROOT / "gnuradio"
DECODERS = GNURADIO / "decoders"
PUBLIC = GNURADIO / "public"
M5 = "AX100 ASM+Golay"

# Exact always-on decoder scope. Each tuple is
# (baudrate, peak deviation Hz, framing, nominal RF frequency Hz).
EXPECTED = {
    "MAVERIC_DECODER.yml": {
        (9600, 3200, M5, 437_575_000),
    },
    "ROADS_DECODER.yml": {
        (4800, 1200, M5, 435_400_000),
        (9600, 2400, M5, 435_400_000),
    },
    "SUOMI100_DECODER.yml": {
        (9600, 2400, M5, 437_775_000),
    },
    "LUOJIA1_DECODER.yml": {
        (4800, 1600, M5, 437_250_000),
    },
    "CATSAT_DECODER.yml": {
        (2400, 750, M5, 437_185_000),
    },
    "AISTECHSAT2_DECODER.yml": {
        (4800, 1600, M5, 436_730_000),
    },
    "INNOCUBE_DECODER.yml": {
        (9600, 3200, M5, 435_950_000),
    },
    "SNIPE_DECODER.yml": {
        (4800, 1200, M5, 435_450_000),
        (4800, 1600, M5, 435_450_000),
    },
    "NUSHSAT1_DECODER.yml": {
        (1200, 575, M5, 436_200_000),
        (2400, 600, M5, 436_200_000),
        (2400, 800, M5, 436_200_000),
    },
    "SHARJAHSAT_DECODER.yml": {
        (9600, 3000, "AX.25 G3RUH", 437_325_000),
    },
    "ASTROCAST_DECODER.yml": {
        (1200, 1200, "Astrocast FX.25 NRZ-I", 437_150_000),
        (1200, 1200, "Astrocast FX.25 NRZ", 437_150_000),
    },
    "SUCHAI4_DECODER.yml": {
        (4800, 1600, M5, 437_250_000),
    },
}

MISSION_PROFILE = {
    "maveric": "MAVERIC_DECODER.yml",
    "roads": "ROADS_DECODER.yml",
    "suomi100": "SUOMI100_DECODER.yml",
    "luojia1": "LUOJIA1_DECODER.yml",
    "catsat": "CATSAT_DECODER.yml",
    "aistechsat2": "AISTECHSAT2_DECODER.yml",
    "innocube": "INNOCUBE_DECODER.yml",
    "snipe": "SNIPE_DECODER.yml",
    "nushsat1": "NUSHSAT1_DECODER.yml",
    "sharjahsat": "SHARJAHSAT_DECODER.yml",
    "astrocast": "ASTROCAST_DECODER.yml",
    "suchai4": "SUCHAI4_DECODER.yml",
}


def _load(name: str) -> dict:
    doc = yaml.safe_load((DECODERS / name).read_text(encoding="utf-8"))
    assert doc["transmitters"], name
    return doc


def _matrix(doc: dict) -> set[tuple[int, int, str, int]]:
    return {
        (
            int(tx["baudrate"]),
            int(tx["deviation"]),
            str(tx["framing"]),
            int(float(tx["frequency"])),
        )
        for tx in doc["transmitters"].values()
    }


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_decoder_has_exact_production_matrix(name):
    doc = _load(name)
    assert len(doc["transmitters"]) == len(EXPECTED[name])
    assert _matrix(doc) == EXPECTED[name]

    assert isinstance(doc.get("name"), str) and doc["name"].strip()
    assert isinstance(doc.get("norad"), int)
    assert isinstance(doc.get("data"), dict) and doc["data"]
    declared_data = set(doc["data"])
    for transmitter in doc["transmitters"].values():
        assert transmitter["modulation"] == "FSK"
        assert isinstance(transmitter.get("data"), list)
        assert transmitter["data"]
        assert all(item in declared_data for item in transmitter["data"])


def test_every_tracked_decoder_is_an_asserted_production_profile():
    tracked = {path.name for path in DECODERS.glob("*_DECODER.yml")}
    assert tracked == set(EXPECTED)


@pytest.mark.parametrize(
    "mission_id", sorted(set(MISSION_PROFILE) - {"maveric"})
)
def test_mission_build_pins_its_decoder_profile(mission_id, tmp_path):
    platform_cfg: dict = {
        "radio": {"decoder_yml": "gnuradio/decoders/MAVERIC_DECODER.yml"},
    }
    load_mission_spec_from_split(
        platform_cfg, mission_id, {}, data_dir=tmp_path,
    )
    expected = f"gnuradio/decoders/{MISSION_PROFILE[mission_id]}"
    assert platform_cfg["radio"]["decoder_yml"] == expected
    assert (ROOT / expected).is_file()


def test_maveric_seed_pins_single_branch_profile():
    from mav_gss_lib.missions.maveric.mission import _seed

    platform_cfg = {
        "radio": {"decoder_yml": "gnuradio/decoders/ROADS_DECODER.yml"}
    }
    _seed({}, platform_cfg)
    assert platform_cfg["radio"]["decoder_yml"] == (
        "gnuradio/decoders/MAVERIC_DECODER.yml"
    )


def test_selector_defaults_to_maveric_but_never_cross_falls_back(tmp_path):
    from gnuradio.decoder_profiles import resolve_mav_duo_decoder

    decoder_dir = tmp_path / "decoders"
    decoder_dir.mkdir()
    maveric = decoder_dir / "MAVERIC_DECODER.yml"
    maveric.write_text("transmitters: {}\n", encoding="utf-8")
    path, source = resolve_mav_duo_decoder(
        tmp_path, {"GSS_DECODER_YML": "  ", "GSS_MISSION": " maveric "},
    )
    assert Path(path) == maveric
    assert source == "mission maveric"

    with pytest.raises(FileNotFoundError, match="SUOMI100_DECODER.yml"):
        resolve_mav_duo_decoder(tmp_path, {"GSS_MISSION": "suomi100"})


def test_selector_validates_explicit_override(tmp_path):
    from gnuradio.decoder_profiles import resolve_mav_duo_decoder

    override = tmp_path / "custom.yml"
    override.write_text("transmitters: {}\n", encoding="utf-8")
    path, source = resolve_mav_duo_decoder(
        tmp_path, {"GSS_DECODER_YML": str(override), "GSS_MISSION": "roads"},
    )
    assert Path(path) == override
    assert source == "GSS_DECODER_YML"

    with pytest.raises(FileNotFoundError, match="GSS_DECODER_YML"):
        resolve_mav_duo_decoder(
            tmp_path, {"GSS_DECODER_YML": str(tmp_path / "missing.yml")},
        )


def test_decoder_options_match_deframer_family():
    from gnuradio.decoder_profiles import decoder_options

    assert decoder_options(DECODERS / "MAVERIC_DECODER.yml") == (
        "--syncword_threshold 6"
    )
    assert decoder_options(DECODERS / "ASTROCAST_DECODER.yml") == (
        "--syncword_threshold 6"
    )
    assert decoder_options(DECODERS / "SHARJAHSAT_DECODER.yml") == ""


def test_public_maveric_beacon_decoder_matches_flight_profile():
    directory = PUBLIC / "MAVERIC_beacon_decoder"
    doc = yaml.safe_load(
        (directory / "MAVERIC_BEACON.yml").read_text(encoding="utf-8")
    )
    assert _matrix(doc) == {(9600, 3200, M5, 437_575_000)}

    py = (directory / "MAV_BEACON.py").read_text(encoding="utf-8")
    assert 'options = "--syncword_threshold 6"' in py

    grc = yaml.safe_load((directory / "MAV_BEACON.grc").read_text(encoding="utf-8"))
    (decoder,) = [
        block for block in grc["blocks"]
        if block["name"] == "satellites_satellite_decoder_0"
    ]
    assert decoder["parameters"]["options"] == '"--syncword_threshold 6"'


def test_public_maveric_beacon_zip_matches_tracked_package():
    package = PUBLIC / "MAVERIC_beacon_decoder"
    with zipfile.ZipFile(PUBLIC / "MAVERIC_beacon_decoder.zip") as archive:
        archived = set(archive.namelist())
        for path in package.iterdir():
            if not path.is_file():
                continue
            name = f"MAVERIC_beacon_decoder/{path.name}"
            assert name in archived
            assert archive.read(name) == path.read_bytes()
        assert not any("__pycache__" in name for name in archived)
