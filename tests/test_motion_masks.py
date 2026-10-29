import sys
import unittest
import tempfile
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from motion_masks import (MaskRejected, fit_reveal_edge, initialize_mask,
                          propagate_pair, reveal_sequence, track_masks, publish_directory, validate_output)


def synthetic_pass(count=67, width=160, height=96):
    """Opaque textured full-height panel moving independently of a textured background."""
    rng = np.random.default_rng(4812)
    background = rng.integers(70, 170, (height, width, 3), dtype=np.uint8)
    background[..., 2] = 50
    panel = rng.integers(0, 36, (height, 52, 3), dtype=np.uint8)
    panel[..., 2] += 190
    frames, truth = [], []
    for i in range(count):
        x0 = 180-4*i
        a, b = max(0, x0), min(width, x0+52)
        image, mask = background.copy(), np.zeros((height, width), np.uint8)
        if b > a:
            image[:, a:b] = panel[:, a-x0:b-x0]
            mask[:, a:b] = 1
        frames.append(image); truth.append(mask)
    return frames, truth


def settings(mode='reveal'):
    return SimpleNamespace(flow='dis', fb_threshold=2., search_margin=10,
                           refine_iterations=2, mode=mode)


class MotionMaskTests(unittest.TestCase):
    def test_prompted_grabcut_initializes_actual_object(self):
        frames, truth = synthetic_pass()
        mask, _ = initialize_mask(frames[30], [.35, 0, .4, 1], [[.5, .5]], [[.1, .5]], solid=True)
        iou = np.count_nonzero(mask & truth[30])/np.count_nonzero(mask | truth[30])
        self.assertGreater(iou, .98)

    def test_dense_bidirectional_propagation_and_complete_reveal(self):
        frames, truth = synthetic_pass()
        masks, reveals = {}, {}
        reports = track_masks(lambda i: frames[i], len(frames), 30, truth[30], settings(),
                              lambda i, m: masks.__setitem__(i, m.copy()))
        result = reveal_sequence(lambda i: masks[i], reports, 'right',
                                 lambda i, a: reveals.__setitem__(i, a.copy()), require_complete=True)
        ious = [np.count_nonzero(masks[i] & target)/max(1, np.count_nonzero(masks[i] | target))
                for i, target in enumerate(truth) if target.any()]
        self.assertGreater(float(np.mean(ious)), .95)
        self.assertTrue(result['complete'])
        self.assertLess(result['first_b_fraction'], .001)
        self.assertGreater(result['last_b_fraction'], .999)
        self.assertTrue(any('median_fb_error_analysis_pixels' in r for r in reports))
        # Object alpha is a finite band; reveal alpha is the swept side.
        self.assertEqual(float(reveals[30][48, 145]), 1.)
        self.assertEqual(int(masks[30][48, 145]), 0)
        self.assertEqual(float(reveals[30][48, 80]), 0.)
        self.assertEqual(int(masks[30][48, 80]), 1)

    def test_reentry_after_exit_is_rejected(self):
        frames, truth = synthetic_pass()
        frames = frames + frames[55:50:-1]
        with self.assertRaisesRegex(MaskRejected, 'reappeared'):
            track_masks(lambda i: frames[i], len(frames), 30, truth[30], settings(), lambda i, m: None)

    def test_incomplete_pass_is_rejected_without_endpoint_padding(self):
        mask = np.zeros((60, 100), np.uint8); mask[:, 30:60] = 1
        reports = [{'state': 'visible'}, {'state': 'visible'}]
        with self.assertRaisesRegex(MaskRejected, 'does not complete'):
            reveal_sequence(lambda i: mask, reports, 'right', lambda i, a: None, require_complete=True)

    def test_nonspanning_object_cannot_be_used_as_pillar_reveal(self):
        mask = np.zeros((100, 160), np.uint8); mask[30:65, 40:80] = 1
        with self.assertRaisesRegex(MaskRejected, 'spanning'):
            fit_reveal_edge(mask, 'right')

    def test_curved_edge_is_rejected(self):
        mask = np.zeros((120, 180), np.uint8)
        for y in range(120):
            edge = round(100+25*np.sin(y*4*np.pi/120))
            mask[y, 30:edge] = 1
        with self.assertRaisesRegex(MaskRejected, 'No dominant straight'):
            fit_reveal_edge(mask, 'right')

    def test_direction_reversal_is_rejected_without_smoothing_it_away(self):
        masks = []
        for x in [20, 32]:
            mask = np.zeros((60, 100), np.uint8); mask[:, x:x+30] = 1; masks.append(mask)
        with self.assertRaisesRegex(MaskRejected, 'reverses direction'):
            reveal_sequence(lambda i: masks[i], [{'state': 'visible'}]*2, 'right', lambda i, a: None)

    def test_object_disappearance_in_middle_is_rejected(self):
        frames, truth = synthetic_pass()
        # The whole panel disappears through a cut, away from every border.
        with self.assertRaises(MaskRejected):
            propagate_pair(frames[30], frames[0], truth[30], settings())

    def test_empty_mask_without_exit_evidence_is_rejected(self):
        with self.assertRaisesRegex(MaskRejected, 'border-exit evidence'):
            reveal_sequence(lambda i: np.zeros((60, 100), np.uint8),
                            [{'state': 'visible'}]*2, 'right', lambda i, a: None)

    def test_low_texture_cut_does_not_become_a_tracked_object(self):
        old = np.full((96, 160, 3), (90, 140, 40), np.uint8)
        old[:, 60:112] = (20, 25, 210)
        current = np.full_like(old, (90, 140, 40))
        mask = np.zeros(old.shape[:2], np.uint8); mask[:, 60:112] = 1
        with self.assertRaises(MaskRejected):
            propagate_pair(old, current, mask, settings())

    def test_publication_failure_restores_previous_bundle(self):
        import motion_masks
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            previous, payload, backup = root/'old', root/'new', root/'backup'
            previous.mkdir(); payload.mkdir()
            (previous/'value').write_text('old')
            (payload/'value').write_text('new')
            original = motion_masks.os.replace
            def fail_new(source, target):
                if source == payload: raise OSError('injected failure')
                return original(source, target)
            with patch.object(motion_masks.os, 'replace', side_effect=fail_new):
                with self.assertRaises(OSError):
                    publish_directory(payload, previous, backup)
            self.assertEqual((previous/'value').read_text(), 'old')
            self.assertFalse(backup.exists())

    def test_output_cannot_contain_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); source = root/'source.mp4'; source.write_bytes(b'video')
            with self.assertRaisesRegex(Exception, 'contain or alias'):
                validate_output(root, [source], True)

    def test_all_directions_have_correct_swept_side(self):
        mask = np.zeros((100, 100), np.uint8); mask[:, 30:60] = 1
        right, _ = fit_reveal_edge(mask, 'right')
        left, _ = fit_reveal_edge(mask, 'left')
        up, _ = fit_reveal_edge(mask.T, 'up')
        down, _ = fit_reveal_edge(mask.T, 'down')
        self.assertAlmostEqual(float(right.mean()), .4, places=6)
        self.assertAlmostEqual(float(left.mean()), .3, places=6)
        self.assertAlmostEqual(float(up.mean()), .3, places=6)
        self.assertAlmostEqual(float(down.mean()), .4, places=6)


if __name__ == '__main__':
    unittest.main()
