"""Verifies Framer.log_fields / log_line aggregate correctly through FramerChain."""
import unittest

from mav_gss_lib.platform.framing import build_chain


class FramerLogAggregationTests(unittest.TestCase):
    def test_csp_plus_asm_golay_log_fields(self):
        chain = build_chain([
            {"framer": "csp_v1", "config": {"enabled": True, "src": 6, "dest": 8}},
            {"framer": "asm_golay"},
        ])
        fields = chain.log_fields()
        self.assertEqual(fields["facts"]["protocol"]["csp_header"]["src"], 6)
        self.assertEqual(fields["facts"]["protocol"]["csp_header"]["dest"], 8)

    def test_log_lines_skips_disabled_framers(self):
        chain = build_chain([
            {"framer": "csp_v1", "config": {"enabled": False}},
            {"framer": "asm_golay"},
        ])
        self.assertEqual(chain.log_lines(), [])

    def test_log_lines_innermost_first(self):
        chain = build_chain([
            {"framer": "csp_v1", "config": {"enabled": True, "src": 6, "dest": 8}},
            {"framer": "asm_golay"},
        ])
        lines = chain.log_lines()
        self.assertTrue(lines and "CSP" in lines[0])

    def test_ax25_log_fields(self):
        chain = build_chain([
            {"framer": "ax25", "config": {"enabled": True, "src_call": "WAAAAA", "dest_call": "WBBBBB"}},
        ])
        fields = chain.log_fields()
        self.assertEqual(fields["facts"]["protocol"]["ax25_header"]["src_call"], "WAAAAA")


if __name__ == "__main__":
    unittest.main()
