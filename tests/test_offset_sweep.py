import unittest

from mav_gss_lib.platform.tracking import OffsetSweep, OffsetSweepConfig


class OffsetSweepTests(unittest.TestCase):
    def test_zigzag_order_from_zero_base(self) -> None:
        sweep = OffsetSweep(OffsetSweepConfig(base_hz=0.0, step_hz=1.0, max_deviation_hz=3.0))
        offsets = [sweep.current_offset_hz]
        for _ in range(6):
            offsets.append(sweep.advance())
        self.assertEqual(offsets, [0.0, 1.0, -1.0, 2.0, -2.0, 3.0, -3.0])

    def test_wraps_around_after_full_cycle(self) -> None:
        sweep = OffsetSweep(OffsetSweepConfig(base_hz=0.0, step_hz=1.0, max_deviation_hz=2.0))
        # Sequence is [0, 1, -1, 2, -2] — 5 values.
        for _ in range(5):
            sweep.advance()
        self.assertEqual(sweep.current_offset_hz, 0.0)

    def test_recentering_clamps_to_absolute_ceiling_not_relative_to_base(self) -> None:
        # base=2, step=1, max_deviation=3 -> valid absolute values are
        # {-3,-2,-1,0,1,2,3}; base+2*step=4 must be skipped even though
        # it's only 2 away from the base, because it's >3 from zero.
        sweep = OffsetSweep(OffsetSweepConfig(base_hz=2.0, step_hz=1.0, max_deviation_hz=3.0))
        offsets = [sweep.current_offset_hz]
        for _ in range(6):
            offsets.append(sweep.advance())
        self.assertNotIn(4.0, offsets)
        for v in offsets:
            self.assertLessEqual(abs(v), 3.0)
        self.assertEqual(offsets[0], 2.0)

    def test_base_outside_ceiling_gets_clamped_into_range(self) -> None:
        sweep = OffsetSweep(OffsetSweepConfig(base_hz=10.0, step_hz=1.0, max_deviation_hz=3.0))
        self.assertEqual(sweep.current_offset_hz, 3.0)

    def test_zero_step_is_a_degenerate_single_value_sequence(self) -> None:
        sweep = OffsetSweep(OffsetSweepConfig(base_hz=1.5, step_hz=0.0, max_deviation_hz=3.0))
        self.assertEqual(sweep.current_offset_hz, 1.5)
        self.assertEqual(sweep.advance(), 1.5)

    def test_zero_max_deviation_is_fully_inert(self) -> None:
        sweep = OffsetSweep(OffsetSweepConfig(base_hz=5.0, step_hz=1.0, max_deviation_hz=0.0))
        self.assertEqual(sweep.current_offset_hz, 0.0)
        self.assertEqual(sweep.advance(), 0.0)

    def test_reset_recenters_and_restarts_index(self) -> None:
        sweep = OffsetSweep(OffsetSweepConfig(base_hz=0.0, step_hz=1.0, max_deviation_hz=3.0))
        sweep.advance()
        sweep.advance()
        self.assertNotEqual(sweep.current_offset_hz, 0.0)
        sweep.reset(OffsetSweepConfig(base_hz=1.0, step_hz=1.0, max_deviation_hz=3.0))
        self.assertEqual(sweep.current_offset_hz, 1.0)


if __name__ == "__main__":
    unittest.main()
