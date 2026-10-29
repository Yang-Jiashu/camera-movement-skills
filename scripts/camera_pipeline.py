#!/usr/bin/env python3
"""Prepare CV assets and render a camera effect in one local, reproducible run.

Initialization is explicit: select the foreground/occluder ROI (or supply an
initial mask), or let feature matching propose cross-shot anchors. All video
timing assumes CFR and tagged HDR must first be converted explicitly. Outputs
are silent. Only accepted assets reach the final render; failed runs preserve
previous outputs. No model weights or cloud APIs are needed by these pipelines.
"""
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
import argparse
import json
import io
import math
import os
import shutil
import subprocess
import sys
import tempfile

import cv2
import numpy as np

import apply_camera_motion
from motion_core import MotionError, Reader, transition_mask_indices
from motion_match import match_images

HERE = Path(__file__).resolve().parent
LAYER_KEYS = ['anchor', 'foreground_scale_start', 'foreground_scale_end',
              'background_scale_start', 'background_scale_end',
              'foreground_shift_start', 'foreground_shift_end',
              'background_shift_start', 'background_shift_end']


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subs = p.add_subparsers(dest='command', required=True)
    for name in ['wipe', 'parallax', 'layered-vertigo', 'match-zoom']:
        s = subs.add_parser(name, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
        s.add_argument('--input', type=Path, required=True, help='Opaque SDR still for layers, or SDR/CFR video for transitions.')
        s.add_argument('--output', type=Path, required=True, help='Final silent MP4; published only after preparation and decode checks pass.')
        s.add_argument('--report', type=Path, help='JSON summary; default is output filename with .json extension.')
        s.add_argument('--assets-dir', type=Path, help='Default: OUTPUT stem + .assets. Includes preparation QA and render report.')
        s.add_argument('--ffmpeg', default=shutil.which('ffmpeg'), help='FFmpeg executable path; required if not on PATH.')
        s.add_argument('--overwrite', action='store_true', help='Replace the complete previous video/report/asset bundle on success.')
        s.add_argument('--width', type=int, default=1280, help='Even output width, pixels.')
        s.add_argument('--height', type=int, default=720, help='Even output height, pixels.')
        s.add_argument('--fps', type=float, default=30, help='Output/mask samples per second; CFR frame-index timing.')
        s.add_argument('--duration', type=float, default=4, help='Final output seconds, rounded to a whole frame count.')
        s.add_argument('--crf', type=int, default=20, help='H.264 CRF quality, 0 to 51; lower uses more data.')
        if name != 'match-zoom':
            s.add_argument('--roi', nargs=4, type=float, help='Normalized X Y WIDTH HEIGHT in the initialization frame.')
            s.add_argument('--foreground-point', nargs=2, type=float, action='append', default=[], help='Known foreground X Y in normalized initialization-frame coordinates; repeatable.')
            s.add_argument('--background-point', nargs=2, type=float, action='append', default=[], help='Known background X Y in normalized initialization-frame coordinates; repeatable.')
        if name in ['wipe', 'match-zoom']:
            s.add_argument('--input-b', type=Path, required=True, help='Incoming SDR/CFR scene.')
            s.add_argument('--start', type=float, default=0, help='Input A start time, source seconds.')
            s.add_argument('--start-b', type=float, default=0, help='Input B start time, source seconds.')
            s.add_argument('--cut-at', type=float, help='Transition center on final timeline; default duration/2.')
            s.add_argument('--transition', type=float, default=1.1 if name == 'wipe' else .25, help='Transition length, output seconds.')
        if name == 'wipe':
            s.add_argument('--initial-mask', type=Path, help='Optional object alpha at init-frame, never a precomputed reveal sequence.')
            s.add_argument('--init-frame', type=int, help='Index relative to transition start, default middle transition frame.')
            s.add_argument('--direction', choices=['right', 'left', 'up', 'down'], default='right',
                           help='Side from which B is revealed; measured occluder edge must sweep in the matching direction.')
            s.add_argument('--analysis-width', type=int, default=640, help='Maximum mask processing width; final assets return to source size.')
            s.add_argument('--flow', choices=['dis', 'farneback'], default='dis', help='Dense optical-flow estimator used for propagation.')
        elif name in ['parallax', 'layered-vertigo']:
            s.add_argument('--alpha', type=Path, help='Optional supplied static alpha; otherwise ROI and seeds initialize segmentation.')
            s.add_argument('--backend', choices=['telea', 'ns'], default='telea', help='Classical limited-background inpainting algorithm.')
            s.add_argument('--working-width', type=int, default=1920, help='Downscale layer source consistently to this maximum width; 0 keeps native size.')
            s.add_argument('--keep-largest', action='store_true', help='Explicitly retain only the largest foreground component.')
            s.add_argument('--fill-holes-up-to', type=int, default=0, help='Explicitly fill enclosed mask holes up to this working-pixel area.')
            s.add_argument('--strict-motion', action='store_true', help='Reject a request outside the repair budget instead of reducing it with a report.')
            s.add_argument('--anchor', type=float, nargs=2, default=[.5, .5], help='Corresponding normalized input/output pivot X Y.')
            for layer in ['foreground', 'background']:
                s.add_argument(f'--{layer}-scale-start', type=float, default=1.15, help='Initial layer magnification relative to covering the output.')
                s.add_argument(f'--{layer}-scale-end', type=float, default=1.15 if name == 'parallax' or layer == 'foreground' else 1.06, help='Final layer magnification; approved value is recorded.')
                s.add_argument(f'--{layer}-shift-start', type=float, nargs=2, default=[0., 0.], help='Initial X Y displacement as fractions of output width/height.')
                default = ([.035, 0.] if layer == 'foreground' else [-.015, 0.]) if name == 'parallax' else [0., 0.]
                s.add_argument(f'--{layer}-shift-end', type=float, nargs=2, default=default, help='Final X Y displacement as fractions of output width/height.')
        else:
            s.add_argument('--anchor-a', type=float, nargs=2, help='Optional normalized A anchor; provide B anchor too. Omit both for SIFT registration.')
            s.add_argument('--anchor-b', type=float, nargs=2, help='Optional normalized B anchor corresponding to A.')
            s.add_argument('--analysis-width', type=int, default=960, help='Maximum feature registration width, pixels.')
            s.add_argument('--base-zoom', type=float, default=1.05, help='Wide-view relative crop magnification.')
            s.add_argument('--peak-zoom', type=float, default=3., help='Transition-peak relative crop magnification.')
    return p


def publish_artifacts(artifacts):
    """Stage files/directories beside targets, restore previous versions on ordinary errors.

    This is not a cross-file atomic transaction and does not survive every OS
    crash. Each rename is atomic; no concurrent external writer is coordinated.
    """
    with ExitStack() as stack:
        prepared, changed = [], []
        for source, destination in artifacts:
            parent = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='.camera-publish-', dir=destination.parent)))
            new, old = parent/'new', parent/'old'
            if source.is_dir(): shutil.copytree(source, new)
            else: shutil.copy2(source, new)
            prepared.append((new, destination, old))
        try:
            for new, destination, old in prepared:
                existed = destination.exists()
                if existed: os.replace(destination, old)
                changed.append((destination, old if existed else None))
                os.replace(new, destination)
        except BaseException:
            for destination, old in reversed(changed):
                if destination.is_dir(): shutil.rmtree(destination)
                elif destination.exists(): destination.unlink()
                if old is not None: os.replace(old, destination)
            raise


def relocate(value, source, destination):
    if isinstance(value, str): return value.replace(str(source), str(destination))
    if isinstance(value, list): return [relocate(x, source, destination) for x in value]
    if isinstance(value, dict): return {k: relocate(v, source, destination) for k, v in value.items()}
    return value


def append_option(command, name, value):
    command.append('--'+name.replace('_', '-'))
    command.extend(str(x) for x in value) if isinstance(value, (list, tuple)) else command.append(str(value))


def run_preparation(script, arguments):
    result = subprocess.run([sys.executable, str(HERE/script), *arguments], capture_output=True, text=True)
    if result.returncode:
        raise MotionError(f'{script} rejected the preparation:\n{result.stderr[-4500:]}')


def validate(a):
    a.ffmpeg = shutil.which(str(a.ffmpeg)) or a.ffmpeg
    if not a.ffmpeg or not Path(a.ffmpeg).is_file(): raise MotionError('Supply --ffmpeg or put FFmpeg on PATH.')
    if not a.input.is_file(): raise MotionError('Input does not exist.')
    if a.output.suffix.lower() != '.mp4': raise MotionError('Output must be MP4.')
    a.report = a.report or a.output.with_suffix('.json')
    if a.report.suffix.lower() != '.json': raise MotionError('Pipeline report must be JSON.')
    a.assets_dir = a.assets_dir or a.output.with_suffix('.assets')
    if min(a.width, a.height) < 2 or a.width % 2 or a.height % 2 or not 0 <= a.crf <= 51:
        raise MotionError('Use positive even output dimensions and CRF in [0,51].')
    if hasattr(a, 'input_b') and not a.input_b.is_file(): raise MotionError('Second input does not exist.')
    if not np.isfinite([a.duration, a.fps]).all() or min(a.duration, a.fps) <= 0:
        raise MotionError('Duration and fps must be finite and positive.')
    count = round(a.duration*a.fps)
    if count < 2: raise MotionError('Select at least two output frames.')
    a.duration = count/a.fps
    targets = [a.output.resolve(), a.report.resolve(), a.assets_dir.resolve()]
    if len(set(targets)) != 3: raise MotionError('Output, report and assets directory must have different paths.')
    for target in targets[:2]:
        if target.is_relative_to(targets[2]): raise MotionError('Video and report cannot be inside the assets directory.')
    inputs = [getattr(a, name, None) for name in ['input', 'input_b', 'alpha', 'initial_mask']]
    for path in [a.output, a.report, a.assets_dir]:
        if path.is_symlink(): raise MotionError('Destination symlinks are unsupported.')
        if path.exists() and not a.overwrite: raise MotionError('Output/report/assets already exist; use --overwrite.')
        if path in [a.output, a.report] and path.exists() and not path.is_file(): raise MotionError('Video/report target must be a regular file.')
        if path == a.assets_dir and path.exists() and not path.is_dir(): raise MotionError('Assets target must be a directory.')
        for source in filter(None, inputs):
            if source.resolve() == path.resolve() or source.resolve().is_relative_to(path.resolve()):
                raise MotionError('A destination cannot replace or contain an input.')
            if source.exists() and path.exists() and source.samefile(path): raise MotionError('Output cannot alias an input.')
        path.parent.mkdir(parents=True, exist_ok=True)
    if a.command in ['wipe', 'match-zoom']:
        a.cut_at = a.cut_at if a.cut_at is not None else a.duration/2
        if (not np.isfinite([a.start, a.start_b, a.cut_at, a.transition]).all()
                or min(a.start, a.start_b) < 0 or a.transition <= 0
                or a.cut_at-a.transition/2 < 0 or a.cut_at+a.transition/2 > a.duration):
            raise MotionError('Transition must fit entirely inside the output interval.')


def run(a):
    validate(a)
    with tempfile.TemporaryDirectory(prefix='.camera-pipeline-', dir=a.output.parent) as temp:
        work = Path(temp); assets = work/'assets'; assets.mkdir()
        render = [a.command, '--output', str(work/'result.mp4'), '--report', str(assets/'render-report.json'),
                  '--ffmpeg', str(a.ffmpeg), '--width', str(a.width), '--height', str(a.height),
                  '--fps', str(a.fps), '--duration', str(a.duration), '--crf', str(a.crf)]
        preparation = {}
        if a.command in ['parallax', 'layered-vertigo']:
            if a.alpha is None and a.roi is None: raise MotionError('Layer preparation needs --roi or --alpha.')
            cmd = ['--input', str(a.input), '--output-dir', str(assets/'layers'), '--backend', a.backend,
                   '--width', str(a.width), '--height', str(a.height), '--fps', str(a.fps),
                   '--duration', str(a.duration), '--ffmpeg', str(a.ffmpeg)]
            if a.alpha: cmd += ['--alpha', str(a.alpha)]
            elif a.roi: append_option(cmd, 'roi', a.roi)
            append_option(cmd, 'working_width', a.working_width)
            append_option(cmd, 'fill_holes_up_to', a.fill_holes_up_to)
            if a.keep_largest: cmd.append('--keep-largest')
            for name in ['foreground_point', 'background_point']:
                for point in getattr(a, name): append_option(cmd, name, point)
            for key in LAYER_KEYS: append_option(cmd, key, getattr(a, key))
            if a.strict_motion: cmd.append('--strict-motion')
            run_preparation('motion_layers.py', cmd)
            preparation = json.loads((assets/'layers/manifest.json').read_text())
            for key in ['foreground', 'background', 'alpha']:
                render += ['--'+key, str(assets/'layers'/f'{key}.png')]
            approved = preparation['approved_motion']
            for key in LAYER_KEYS:
                value = approved.get(key, approved.get(key.replace('_', '-')))
                if value is None: raise MotionError('Prepared layers omitted required motion field: '+key)
                append_option(render, key, value)
        elif a.command == 'wipe':
            if a.initial_mask is None and a.roi is None: raise MotionError('Video mask preparation needs --roi or --initial-mask.')
            cut_start = a.cut_at-a.transition/2
            cut_end = a.cut_at+a.transition/2
            mask_indices = transition_mask_indices(round(a.duration*a.fps), a.fps, cut_start, cut_end, a.fps, 0)
            if len(mask_indices) < 2: raise MotionError('Select at least two transition samples.')
            if mask_indices != list(range(len(mask_indices))):
                raise MotionError('Transition sampling did not form a contiguous mask timeline.')
            count = len(mask_indices)
            mask_start = math.ceil((cut_start-1e-7)*a.fps)/a.fps
            # Frame indices are relative to the transition start, not the whole clip.
            init = a.init_frame if a.init_frame is not None else count//2
            cmd = ['--input', str(a.input), '--output-dir', str(assets/'masks'),
                   '--start', str(a.start+mask_start), '--duration', str(count/a.fps), '--fps', str(a.fps),
                   '--init-frame', str(init), '--mode', 'reveal', '--reveal-direction', a.direction,
                   '--analysis-width', str(a.analysis_width), '--flow', a.flow,
                   '--require-complete-reveal', '--ffmpeg', str(a.ffmpeg)]
            if a.initial_mask: cmd += ['--initial-mask', str(a.initial_mask)]
            else: append_option(cmd, 'roi', a.roi)
            for name in ['foreground_point', 'background_point']:
                for point in getattr(a, name): append_option(cmd, name, point)
            run_preparation('motion_masks.py', cmd)
            preparation = json.loads((assets/'masks/manifest.json').read_text())
            render += ['--mask-dir', str(assets/'masks/reveal'), '--mask-fps', str(a.fps), '--direction', a.direction]
        else:
            if bool(a.anchor_a) != bool(a.anchor_b): raise MotionError('Provide both anchor-a and anchor-b, or omit both for automatic matching.')
            if a.anchor_a:
                preparation = dict(method='explicit user anchors', anchor_a=a.anchor_a, anchor_b=a.anchor_b)
            else:
                readers = []
                try:
                    readers.append(Reader(a.input, a.ffmpeg))
                    readers.append(Reader(a.input_b, a.ffmpeg))
                    preparation, montage = match_images(readers[0].at(a.start+a.cut_at),
                        readers[1].at(a.start_b+a.transition/2), a.analysis_width)
                    cv2.imwrite(str(assets/'feature-matches.jpg'), montage)
                finally:
                    for reader in readers: reader.close()
                (assets/'matching.json').write_text(json.dumps(preparation, indent=2)+'\n')
            for key in ['anchor_a', 'anchor_b']: append_option(render, key, preparation[key])
            render += ['--base-zoom', str(a.base_zoom), '--peak-zoom', str(a.peak_zoom)]
        if a.command in ['wipe', 'match-zoom']:
            for key in ['input', 'input_b', 'start', 'start_b', 'cut_at', 'transition']:
                append_option(render, key, getattr(a, key))
        with redirect_stdout(io.StringIO()):
            apply_camera_motion.run(apply_camera_motion.parser().parse_args(render))
        for path in assets.rglob('*.json'):
            data = relocate(json.loads(path.read_text()), assets, a.assets_dir.resolve())
            data = relocate(data, work/'result.mp4', a.output.resolve())
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
        rendered = json.loads((assets/'render-report.json').read_text())
        report = dict(command=a.command, status='rendered; review preparation QA and final visuals',
                      preparation=relocate(preparation, assets, a.assets_dir.resolve()),
                      render_validation=rendered['validation'], assets=str(a.assets_dir.resolve()),
                      audio='silent', timing='CFR frame-index processing; VFR not detected',
                      content='Local classical CV; prepared/inpainted pixels and 2.5D geometry are not recovered physical scene truth.')
        report_path = work/'pipeline.json'
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
        publish_artifacts([(work/'result.mp4', a.output), (report_path, a.report), (assets, a.assets_dir)])
    print(json.dumps(dict(output=str(a.output), report=str(a.report), assets=str(a.assets_dir))))


def main():
    p = parser(); a = p.parse_args()
    try: run(a)
    except (MotionError, cv2.error, OSError, KeyError, ValueError) as exc:
        p.exit(2, f'camera pipeline: {exc}\n')


if __name__ == '__main__': main()
