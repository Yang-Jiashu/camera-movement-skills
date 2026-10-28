"""Ground-truth CV checks: motion recovery, stabilization, rejection and masks."""
from pathlib import Path
from types import SimpleNamespace
import sys
import os
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from motion_core import MotionError
from prepare_motion import (analyze_frames, pose_path, rejected_intervals,
                            residual_metrics, smooth_path, stabilization_maps,
                            static_grabcut, publish_bundle)


def options():
    return SimpleNamespace(analysis_width=384, background_roi=None, max_features=500,
        min_points=12, min_inlier_ratio=.55, min_coverage=.08,
        fb_threshold=1., ransac_threshold=1.5)


def jitter_sequence(count=48):
    rng = np.random.default_rng(1701)
    texture = rng.integers(20, 235, (240, 384, 3), dtype=np.uint8)
    texture = cv2.GaussianBlur(texture, (3, 3), .6)
    frames, offsets = [], []
    for i in range(count):
        dx = .20*i+5*np.sin(2*np.pi*i/6)
        dy = 3*np.cos(2*np.pi*i/5)
        matrix = np.float32([[1, 0, dx], [0, 1, dy]])
        frames.append(cv2.warpAffine(texture, matrix, (384, 240),
            flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101))
        offsets.append([dx, dy])
    return frames, np.array(offsets)


class PreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cv2.setNumThreads(2)
        cls.frames, cls.offsets = jitter_sequence()
        cls.result = analyze_frames(iter(cls.frames), options())

    def test_recovers_known_subpixel_image_translation(self):
        self.assertTrue(all(row['accepted'] for row in self.result['records']))
        path = pose_path(self.result['poses'], self.result['analysis_size'])
        measured = path[:, :2]-path[0, :2]
        truth = self.offsets-self.offsets[0]
        rms = float(np.sqrt(np.mean((measured-truth)**2)))
        self.assertLess(rms, .35)

    def test_stabilization_reduces_retracked_jitter_with_valid_pixels(self):
        maps, report = stabilization_maps(self.result, (320, 200), 24, .15, .95, 1.12, 1.5, True)
        stable = [cv2.warpAffine(frame, matrix, (320, 200),
            flags=cv2.INTER_CUBIC | cv2.WARP_INVERSE_MAP) for frame, matrix in zip(self.frames, maps)]
        after = analyze_frames(iter(stable), options())
        self.assertTrue(all(row['accepted'] for row in after['records']))
        before_path = pose_path(self.result['poses'], self.result['analysis_size'])
        after_path = pose_path(after['poses'], after['analysis_size'])
        before = residual_metrics(before_path, smooth_path(before_path, .15*24))['translation_rms_analysis_pixels']
        measured_after = residual_metrics(after_path, smooth_path(after_path, .15*24))['translation_rms_analysis_pixels']
        # Account for resolution and uniform crop magnification in the retrack.
        after_in_source_units = measured_after*(384/320)/report['zoom']
        self.assertLess(after_in_source_units, before*.35)
        self.assertGreaterEqual(report['minimum_source_pixel_margin'], 1.999)

    def test_no_auto_crop_rejects_unsafe_stabilization(self):
        with self.assertRaises(MotionError):
            stabilization_maps(self.result, (384, 240), 24, .15, 1., 1., 1., False)

    def test_textureless_motion_is_reported_as_failure(self):
        frames = [np.full((120, 192, 3), 127, np.uint8) for _ in range(6)]
        result = analyze_frames(iter(frames), options())
        self.assertFalse(any(row['accepted'] for row in result['records']))
        self.assertEqual(rejected_intervals(result['records'], 24)[0]['start_frame'], 1)
        self.assertEqual(rejected_intervals(result['records'], 24)[0]['end_frame_exclusive'], 6)

    def test_grabcut_matches_known_color_foreground(self):
        image = np.full((192, 256, 3), [170, 90, 20], np.uint8)
        truth = np.zeros((192, 256), np.uint8)
        cv2.circle(truth, (128, 96), 40, 255, -1)
        image[truth > 0] = [20, 30, 220]
        mask, report = static_grabcut(image, [.25, .18, .5, .65],
            [[.5, .5]], [[.26, .20]], iterations=3)
        overlap = np.count_nonzero((mask > 127) & (truth > 0))
        union = np.count_nonzero((mask > 127) | (truth > 0))
        self.assertGreater(overlap/union, .98)
        self.assertFalse(report['learned_segmentation'])
        self.assertFalse(report['temporal_propagation'])

    def test_invalid_foreground_seed_is_rejected(self):
        image = np.zeros((100, 160, 3), np.uint8)
        with self.assertRaises(MotionError):
            static_grabcut(image, [.2, .2, .6, .6], [[.02, .02]])

    def test_second_artifact_failure_restores_both_old_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output, report = root/'mask.png', root/'mask.json'
            source, details = root/'new.png', root/'new.json'
            output.write_bytes(b'old image'); report.write_bytes(b'old report')
            source.write_bytes(b'new image'); details.write_bytes(b'new report')
            replace = os.replace
            calls = [0]
            def fail_second(a, b):
                calls[0] += 1
                if calls[0] == 2: raise OSError('simulated second-file publication failure')
                return replace(a, b)
            with patch('prepare_motion.os.replace', side_effect=fail_second):
                with self.assertRaises(OSError):
                    publish_bundle([(source, output), (details, report)])
            self.assertEqual(output.read_bytes(), b'old image')
            self.assertEqual(report.read_bytes(), b'old report')

    def test_staging_failure_does_not_replace_existing_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output, source = root/'mask.png', root/'new.png'
            output.write_bytes(b'old image'); source.write_bytes(b'new image')
            invalid_report = root/'directory.json'; invalid_report.mkdir()
            with self.assertRaises(MotionError):
                publish_bundle([(source, output), (source, invalid_report)])
            self.assertEqual(output.read_bytes(), b'old image')


if __name__ == '__main__':
    unittest.main()
