#!/usr/bin/env python3
"""The matcher has to report where the control *is*, not where the search window was slid to.

Regression cover for a bug that put every aim at (0,0): the fine pass scores the 44x44
window ``big[y0:y0+h, x0:x0+w]``, so ``_ncc`` returns coordinates *inside that window*. For
a 1:1 hit those are (0,0) and they were recorded as full-screen coordinates, which turned a
correct score into a click on the top-left corner of the screen.

Pure arrays, no browser, no screenshots: the control is pasted at a known offset into a
blank page and the matcher is asked where it is.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

try:
    from PIL import Image
except ImportError:  # pragma: no cover - only the browser pass needs Pillow
    raise unittest.SkipTest("Pillow is not installed")

try:
    import numpy as np
except ImportError:  # pragma: no cover - the browser pass already needs it
    raise unittest.SkipTest("numpy is not installed")

from fetch_browser_paper import _match_biggest, SCORE_THRESHOLD  # noqa: E402

ASSET = Path(__file__).resolve().parent.parent / "assets" / "turnstile_checkbox_unchecked.png"
BLANK = 250.0  # a near-white page, the same thing the real wall sits on


def template():
    with Image.open(ASSET) as img:
        return np.asarray(img.convert("L"), dtype=np.float32)


def page_with(template, size, place):
    big = np.full(size, BLANK, dtype=np.float32)
    y, x = place
    big[y:y + template.shape[0], x:x + template.shape[1]] = template
    return big


class TestMatchCoordinates(unittest.TestCase):
    def setUp(self):
        self.tpl = template()
        self.h, self.w = self.tpl.shape

    def test_reports_the_position_the_control_was_painted_at(self):
        for place in ((137, 291), (0, 0), (10, 20), (640, 650), (300, 5)):
            with self.subTest(place=place):
                big = page_with(self.tpl, (900, 900), place)
                y, x, score = _match_biggest(big, self.tpl)
                self.assertEqual((int(y), int(x)), place,
                                 "the fine pass must return full-screen coordinates")
                self.assertGreaterEqual(score, SCORE_THRESHOLD)

    def test_crop_relative_hit_is_shifted_back_by_the_window_origin(self):
        """The exact case the bug broke: a hit at the window's own origin (0,0)."""
        place = (256, 256)
        big = page_with(self.tpl, (512, 512), place)
        y, x, _ = _match_biggest(big, self.tpl)
        self.assertEqual((int(y), int(x)), place)

    def test_absent_control_is_not_reported(self):
        big = np.full((400, 400), BLANK, dtype=np.float32)
        _, _, score = _match_biggest(big, self.tpl)
        self.assertLess(score, SCORE_THRESHOLD)

    def test_control_smaller_than_template_is_not_reported(self):
        big = np.full((10, 10), BLANK, dtype=np.float32)
        _, _, score = _match_biggest(big, self.tpl)
        self.assertLess(score, SCORE_THRESHOLD)


if __name__ == "__main__":
    unittest.main(verbosity=2)
