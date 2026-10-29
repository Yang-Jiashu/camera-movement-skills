"""Observable destination protection and multi-artifact recovery."""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from camera_pipeline import parser, validate, publish_artifacts
from motion_core import MotionError


class CameraPipelineTests(unittest.TestCase):
    def test_late_directory_publication_failure_restores_every_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); source = root/'source'; source.mkdir()
            out = root/'old.mp4'; out.write_bytes(b'old video')
            report = root/'old.json'; report.write_bytes(b'old report')
            assets = root/'old.assets'; assets.mkdir(); (assets/'mask.png').write_bytes(b'old asset')
            video = source/'new.mp4'; video.write_bytes(b'new video')
            details = source/'new.json'; details.write_bytes(b'new report')
            new_assets = source/'assets'; new_assets.mkdir(); (new_assets/'mask.png').write_bytes(b'new asset')
            replace = os.replace; calls = [0]
            def fail_last(a, b):
                calls[0] += 1
                if calls[0] == 6: raise OSError('simulated final asset directory failure')
                return replace(a, b)
            with patch('camera_pipeline.os.replace', side_effect=fail_last):
                with self.assertRaises(OSError):
                    publish_artifacts([(video, out), (details, report), (new_assets, assets)])
            self.assertEqual(out.read_bytes(), b'old video')
            self.assertEqual(report.read_bytes(), b'old report')
            self.assertEqual((assets/'mask.png').read_bytes(), b'old asset')

    def test_asset_destination_cannot_contain_input(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); image = root/'photo.png'; image.write_bytes(b'image')
            a = parser().parse_args(['parallax', '--input', str(image), '--output', str(root.parent/'out.mp4'),
                                     '--assets-dir', str(root), '--roi', '.2', '.2', '.4', '.4',
                                     '--overwrite', '--ffmpeg', sys.executable])
            with self.assertRaises(MotionError): validate(a)
            self.assertEqual(image.read_bytes(), b'image')

    def test_odd_dimensions_are_rejected_before_asset_work(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); image = root/'photo.png'; image.write_bytes(b'image')
            a = parser().parse_args(['parallax', '--input', str(image), '--output', str(root/'out.mp4'),
                                     '--width', '319', '--roi', '.2', '.2', '.4', '.4', '--ffmpeg', sys.executable])
            with self.assertRaises(MotionError): validate(a)
            self.assertFalse((root/'out.assets').exists())


if __name__ == '__main__': unittest.main()
