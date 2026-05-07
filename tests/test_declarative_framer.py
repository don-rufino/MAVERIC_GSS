"""DeclarativeFramer behavior tests:
  * Operator-key alias normalization (priority -> prio, source -> src, ...)
    happens at the FRAMERS registry, not in DeclarativeFramer.
  * Live mission_cfg edits propagate to the next frame() call.
  * Static `config:` overrides `config_ref:` values when both are present.
"""
import unittest

from mav_gss_lib.platform.contract.commands import EncodedCommand
from mav_gss_lib.platform.framing import DeclarativeFramer, build_chain
from mav_gss_lib.platform.spec import FramerSpec, FramingSpec


def _asm_golay_spec() -> FramingSpec:
    return FramingSpec(
        uplink_chain=(
            FramerSpec(framer="csp_v1", config_ref="csp"),
            FramerSpec(framer="asm_golay"),
        ),
        uplink_label="ASM+Golay",
        accept_frame_types=("ASM+GOLAY",),
    )


class DeclarativeFramerTests(unittest.TestCase):
    def test_operator_keys_translate_via_registry(self):
        # mission_cfg uses operator-friendly keys; the registry's
        # _normalize_csp_keys translates them when constructing CSPv1Framer.
        chain = build_chain([
            {"framer": "csp_v1", "config": {
                "enabled": True, "priority": 2, "source": 6,
                "destination": 8, "dest_port": 24, "src_port": 1,
            }},
        ])
        # Inspect the constructed framer's CSPConfig:
        csp = chain.framers[0].config  # type: ignore[attr-defined]
        self.assertEqual(csp.src, 6)
        self.assertEqual(csp.dest, 8)
        self.assertEqual(csp.dport, 24)
        self.assertEqual(csp.sport, 1)

    def test_frames_with_csp_from_mission_cfg(self):
        cfg = {"csp": {
            "enabled": True, "priority": 2, "source": 6, "destination": 8,
            "dest_port": 24, "src_port": 0, "flags": 0, "csp_crc": True,
        }}
        framer = DeclarativeFramer(_asm_golay_spec(), cfg)
        framed = framer.frame(EncodedCommand(raw=b"\x01\x02\x03"))
        self.assertEqual(framed.frame_label, "ASM+Golay")
        self.assertNotIn("uplink_mode", framed.log_fields)  # label lives on FramedCommand.frame_label, not log_fields
        self.assertEqual(framed.log_fields["facts"]["protocol"]["csp_header"]["src"], 6)
        self.assertGreater(framed.max_payload, 0)

    def test_live_config_propagates(self):
        cfg = {"csp": {"enabled": True, "source": 6, "destination": 8}}
        framer = DeclarativeFramer(_asm_golay_spec(), cfg)
        before = framer.frame(EncodedCommand(raw=b"\x01"))
        cfg["csp"]["source"] = 7
        after = framer.frame(EncodedCommand(raw=b"\x01"))
        self.assertEqual(before.log_fields["facts"]["protocol"]["csp_header"]["src"], 6)
        self.assertEqual(after.log_fields["facts"]["protocol"]["csp_header"]["src"], 7)

    def test_static_config_overrides_config_ref(self):
        spec = FramingSpec(
            uplink_chain=(
                FramerSpec(framer="csp_v1",
                           config={"enabled": True, "source": 99},
                           config_ref="csp"),
                FramerSpec(framer="asm_golay"),
            ),
            uplink_label="ASM+Golay",
        )
        cfg = {"csp": {"enabled": True, "source": 6}}
        framed = DeclarativeFramer(spec, cfg).frame(EncodedCommand(raw=b"\x01"))
        # Static `config: {source: 99}` wins over `config_ref` lookup.
        self.assertEqual(framed.log_fields["facts"]["protocol"]["csp_header"]["src"], 99)


if __name__ == "__main__":
    unittest.main()
