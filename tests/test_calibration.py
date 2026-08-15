from __future__ import annotations

import unittest

from tvsub_mcp.calibration import calculate


class CalibrationTests(unittest.TestCase):
    def test_no_anchor_is_identity(self) -> None:
        result = calculate([])
        self.assertEqual(result.scale, 1.0)
        self.assertEqual(result.offset, 0.0)

    def test_one_anchor_is_offset_only(self) -> None:
        result = calculate([{"subTime": 100, "actualTime": 107.27}])
        self.assertEqual(result.scale, 1.0)
        self.assertAlmostEqual(result.offset, 7.27)

    def test_two_anchor_pal_scale(self) -> None:
        anchors = [
            {"subTime": 100, "actualTime": 100 * 1.0427 + 3},
            {"subTime": 7000, "actualTime": 7000 * 1.0427 + 3},
        ]
        result = calculate(anchors)
        self.assertAlmostEqual(result.scale, 1.0427, places=6)
        self.assertAlmostEqual(result.offset, 3.0, places=6)
        self.assertIsNone(result.warning)

    def test_close_anchors_warn(self) -> None:
        result = calculate([
            {"subTime": 10, "actualTime": 12},
            {"subTime": 20, "actualTime": 22},
        ])
        self.assertIn("불안정", result.warning or "")


if __name__ == "__main__":
    unittest.main()

