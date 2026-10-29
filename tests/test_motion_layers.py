"""Behavioral tests for real layer preparation and bounded visible inpainting."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest

import cv2
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from motion_layers import LayerMotion, clean_alpha, prepare_layers, exposure_budget
from motion_core import MotionError


def scene():
    y,x=np.mgrid[:270,:480]
    true_background=np.uint8(np.stack([80+.08*x,110+.1*y,65+.08*x+.06*y],axis=2))
    alpha=np.zeros((270,480),np.uint8)
    cv2.rectangle(alpha,(175,85),(300,235),255,-1)
    image=true_background.copy();image[alpha>0]=[25,30,225]
    return image,alpha,true_background


class LayerPreparationTests(unittest.TestCase):
    def test_inputs_cannot_be_replaced_through_hardlink_output_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);image,alpha,_=scene()
            cv2.imwrite(str(p/'scene.png'),image);cv2.imwrite(str(p/'mask.png'),alpha)
            (p/'layers').mkdir()
            os.link(p/'mask.png',p/'layers/alpha.png')
            before=(p/'mask.png').read_bytes()
            with self.assertRaisesRegex(MotionError,'alias'):
                prepare_layers(p/'scene.png',p/'layers',alpha_path=p/'mask.png',overwrite=True)
            self.assertEqual((p/'mask.png').read_bytes(),before)

    def test_rejected_overwrite_preserves_a_previous_complete_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);image,alpha,_=scene()
            cv2.imwrite(str(p/'scene.png'),image);cv2.imwrite(str(p/'alpha.png'),alpha)
            safe=LayerMotion(width=480,height=270,duration=.5,fps=12,
                foreground_scale_start=1.3,foreground_scale_end=1.3,
                background_scale_start=1.3,background_scale_end=1.3,
                foreground_shift_end=(.005,0))
            prepare_layers(p/'scene.png',p/'layers',motion=safe,alpha_path=p/'alpha.png',
                working_width=480,feather=0)
            before={path.name:path.read_bytes() for path in (p/'layers').iterdir()}
            unsafe=LayerMotion(**{**safe.__dict__,'foreground_shift_end':(.2,0)})
            with self.assertRaisesRegex(MotionError,'preserved'):
                prepare_layers(p/'scene.png',p/'layers',motion=unsafe,alpha_path=p/'alpha.png',
                    working_width=480,feather=0,strict_motion=True,max_fill_distance=5,overwrite=True)
            self.assertEqual(before,{path.name:path.read_bytes() for path in (p/'layers').iterdir()})

    def test_cli_routes_seed_arguments_into_actual_grabcut(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);image,_,_=scene();cv2.imwrite(str(p/'photo.png'),image)
            command=[sys.executable,str(Path(__file__).resolve().parents[1]/'scripts/motion_layers.py'),
                '--input',str(p/'photo.png'),'--output-dir',str(p/'layers'),
                '--roi','.30','.22','.4','.70','--foreground-point','.45','.5',
                '--background-point','.33','.25','--width','480','--height','270',
                '--fps','12','--duration','.5','--working-width','480',
                '--foreground-scale-start','1.1','--foreground-scale-end','1.1',
                '--background-scale-start','1.1','--background-scale-end','1.1',
                '--foreground-shift-end','.008','0']
            result=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn(json.loads(result.stdout)['status'],['accepted','motion_reduced'])

    def test_cleanup_preserves_real_openings_and_small_structures_by_default(self):
        alpha=np.zeros((90,120),np.uint8)
        cv2.rectangle(alpha,(20,20),(80,75),255,-1)
        cv2.rectangle(alpha,(40,35),(50,45),0,-1)
        alpha[8:10,8:10]=255
        result,_=clean_alpha(alpha)
        np.testing.assert_array_equal(result,alpha)
        cleaned,_=clean_alpha(alpha,min_component_area=8,fill_holes_up_to=150)
        self.assertEqual(cleaned[8,8],0)
        self.assertEqual(cleaned[40,45],255)

    def test_supplied_alpha_repairs_a_real_newly_visible_band(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);image,alpha,bg=scene()
            cv2.imwrite(str(p/'scene.png'),image);cv2.imwrite(str(p/'alpha.png'),alpha)
            motion=LayerMotion(width=480,height=270,duration=.5,fps=12,
                foreground_scale_start=1.1,foreground_scale_end=1.1,
                background_scale_start=1.1,background_scale_end=1.1,
                foreground_shift_end=(.012,0))
            report=prepare_layers(p/'scene.png',p/'layers',motion=motion,alpha_path=p/'alpha.png',
                working_width=480,feather=0,mask_expand=1,max_fill_distance=15)
            self.assertEqual(report['status'],'accepted')
            repair=cv2.imread(str(p/'layers/repair-mask.png'),0)>0
            self.assertTrue(repair.any())
            restored=cv2.imread(str(p/'layers/background.png'))
            # The original foreground was red; actual inpainting should draw on
            # surrounding background rather than retain a red ghost in the band.
            before=float(np.mean(np.abs(image[repair].astype(float)-bg[repair])))
            after=float(np.mean(np.abs(restored[repair].astype(float)-bg[repair])))
            self.assertLess(after,before*.35)
            self.assertEqual(cv2.imread(str(p/'layers/alpha.png'),0).shape,alpha.shape)
            self.assertTrue((p/'layers/qa-contact.jpg').is_file())
            approved=LayerMotion(**report['approved_motion'])
            data,_,required=exposure_budget(alpha,cv2.dilate(alpha,np.ones((3,3),np.uint8)),approved)
            validity=cv2.imread(str(p/'layers/background-validity.png'),0)
            self.assertFalse(np.any((required>0)&(validity==0)))

    def test_strict_motion_rejects_an_unsupported_hole_without_emitting_fake_layers(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);image,alpha,_=scene()
            cv2.imwrite(str(p/'scene.png'),image);cv2.imwrite(str(p/'alpha.png'),alpha)
            motion=LayerMotion(width=480,height=270,duration=.5,fps=12,
                foreground_scale_start=1.3,foreground_scale_end=1.3,
                background_scale_start=1.3,background_scale_end=1.3,
                foreground_shift_end=(.2,0))
            report=prepare_layers(p/'scene.png',p/'layers',motion=motion,alpha_path=p/'alpha.png',
                working_width=480,feather=0,strict_motion=True,max_fill_distance=5)
            self.assertEqual(report['status'],'rejected')
            self.assertIsNone(report['approved_motion'])
            self.assertFalse((p/'layers/background.png').exists())
            self.assertEqual(set(x['motion_factor'] for x in report['budget_attempts']),{1.})

    def test_motion_reduction_is_explicit_and_its_full_motion_budget_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);image,alpha,_=scene()
            cv2.imwrite(str(p/'scene.png'),image);cv2.imwrite(str(p/'alpha.png'),alpha)
            motion=LayerMotion(width=480,height=270,duration=.5,fps=12,
                foreground_scale_start=1.3,foreground_scale_end=1.3,
                background_scale_start=1.3,background_scale_end=1.3,
                foreground_shift_end=(.2,0))
            report=prepare_layers(p/'scene.png',p/'layers',motion=motion,alpha_path=p/'alpha.png',
                working_width=480,feather=0,max_fill_distance=8)
            self.assertEqual(report['status'],'motion_reduced')
            self.assertLess(report['motion_factor'],1)
            self.assertLessEqual(report['approved_budget']['maximum_repair_distance'],8)
            self.assertTrue(report['approved_budget']['full_resolution'])
            self.assertTrue(any('REDUCED' in warning for warning in report['warnings']))
            loaded=json.loads((p/'layers/manifest.json').read_text())
            self.assertNotEqual(loaded['requested_motion'],loaded['approved_motion'])

    def test_new_grabcut_assets_are_generated_without_preexisting_layers(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);image,_,_=scene();cv2.imwrite(str(p/'fresh-photo.png'),image)
            report=prepare_layers(p/'fresh-photo.png',p/'layers',roi=(.30,.22,.4,.70),
                foreground_points=[(.45,.5)],background_points=[(.33,.25)],
                motion=LayerMotion(width=480,height=270,duration=.5,fps=12,
                    foreground_scale_start=1.1,foreground_scale_end=1.1,
                    background_scale_start=1.1,background_scale_end=1.1,
                    foreground_shift_end=(.008,0)),working_width=480,analysis_width=480,
                feather=0,max_fill_distance=16)
            self.assertEqual(report['segmentation']['method'],'Static ROI-constrained GrabCut color segmentation')
            alpha=cv2.imread(str(p/'layers/alpha.png'),0)
            self.assertGreater(alpha[150,230],240)
            self.assertLess(alpha[30,50],10)


if __name__=='__main__':unittest.main()
