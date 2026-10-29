"""Independent known-warp checks for automatic visual anchors."""
from pathlib import Path
import sys
import unittest
import cv2
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from motion_core import MotionError
from motion_match import match_images


class MotionMatchTests(unittest.TestCase):
    def test_estimates_anchor_under_known_perspective_warp(self):
        rng = np.random.default_rng(449)
        image = rng.integers(30, 220, (320, 480, 3), dtype=np.uint8)
        image = cv2.GaussianBlur(image, (5, 5), .8)
        cv2.putText(image, 'CAMERA', (70, 160), cv2.FONT_HERSHEY_SIMPLEX, 2, (240, 190, 80), 4)
        h = np.array([[1.02, -.025, 12], [.018, .98, 8], [.00004, -.00002, 1]], np.float64)
        second = cv2.warpPerspective(image, h, (480, 320), borderMode=cv2.BORDER_REFLECT)
        report, _ = match_images(image, second, analysis_width=480)
        a = np.array(report['anchor_a'])*[479, 319]
        expected = cv2.perspectiveTransform(a.astype(np.float32).reshape(1, 1, 2), h)[0, 0]
        actual = np.array(report['anchor_b'])*[479, 319]
        self.assertLess(np.linalg.norm(actual-expected), 1.)
        self.assertGreater(report['inliers'], 30)

    def test_textureless_pair_is_rejected(self):
        image = np.full((180, 320, 3), 128, np.uint8)
        with self.assertRaises(MotionError): match_images(image, image)

    def test_independent_random_textures_are_rejected(self):
        rng = np.random.default_rng(131)
        frames = [rng.integers(0, 255, (240, 360, 3), dtype=np.uint8) for _ in range(2)]
        with self.assertRaises(MotionError): match_images(*frames)


if __name__ == '__main__': unittest.main()
