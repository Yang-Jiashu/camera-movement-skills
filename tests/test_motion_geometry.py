"""Meaningful geometry/time invariants; run with python -m unittest discover -s tests."""
from pathlib import Path
import sys
import unittest

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from motion_core import (MotionError, crop_matrix, normalized_speed_map,
                         rotation_fit_zoom, transition_mask_indices)


class CameraGeometryTests(unittest.TestCase):
    def test_equal_size_without_motion_is_identity(self):
        matrix, data = crop_matrix((640, 360), (640, 360))
        np.testing.assert_allclose(matrix, [[1, 0, 0], [0, 1, 0]], atol=1e-6)
        self.assertEqual(data['output_pixels_per_source_pixel'], 1)

    def test_full_roll_footprints_stay_inside_source(self):
        source, output = (2880, 1620), (1280, 720)
        angles = np.linspace(0, 360, 145)
        zoom = max(rotation_fit_zoom(source, output, angle) for angle in angles)
        corners = np.array([[[0, 0], [1279, 0], [1279, 719], [0, 719]]], np.float32)
        for angle in angles:
            matrix, data = crop_matrix(source, output, zoom=zoom, angle=angle)
            mapped = cv2.transform(corners, matrix)[0]
            self.assertGreaterEqual(mapped.min(), 0)
            self.assertLessEqual(mapped[:, 0].max(), source[0]-1)
            self.assertLessEqual(mapped[:, 1].max(), source[1]-1)
            self.assertLess(data['output_pixels_per_source_pixel'], 1.)

    def test_unsafe_rotation_and_center_are_rejected(self):
        with self.assertRaises(MotionError):
            crop_matrix((1920, 1080), (1280, 720), angle=45)
        with self.assertRaises(MotionError):
            crop_matrix((1920, 1080), (1280, 720), center=(.1, .5))

    def test_clamping_is_explicit_and_reported(self):
        matrix, data = crop_matrix((1920, 1080), (1280, 720), zoom=1.2,
                                   center=(.1, .5), bounds='clamp')
        self.assertTrue(data['center_clamped'])
        self.assertGreater(data['actual_center'][0], .1)
        self.assertGreaterEqual(data['source_edge_margin'], 0)

    def test_speed_profile_integrates_to_selected_source_span(self):
        positions, speeds = normalized_speed_map(120, 8., [[0, 1], [.5, 4], [1, 1]])
        self.assertAlmostEqual(positions[0], 0.)
        self.assertAlmostEqual(positions[-1], 8.)
        self.assertTrue(np.all(np.diff(positions) > 0))
        self.assertGreater(speeds[60], speeds[0])
        with self.assertRaises(MotionError):
            normalized_speed_map(120, 8., [[0, 1], [.8, 0], [1, 1]])

    def test_decimal_transition_does_not_request_an_extra_mask(self):
        masks = transition_mask_indices(120, 30, 2.35-1.1/2, 2.35+1.1/2, 30)
        self.assertEqual(masks, list(range(33)))


if __name__ == '__main__':
    unittest.main()
