#!/usr/bin/env python3
"""Apply camera effects to user-supplied SDR media; no demonstration paths are embedded.

Geometry is 2D unless the caller supplies explicit foreground/background layers.
Every export has a JSON report; source audio is opt-in for unchanged-time commands.
Frame-index timing assumes CFR. Normalize VFR media to CFR before effects when
exact timing or source-audio alignment is required; VFR is not auto-detected.
"""
from pathlib import Path
import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile

import cv2
import numpy as np

from motion_core import (Camera, MotionError, Reader, crop_matrix, ease, mask_path,
                         media_metadata, mix, normalized_speed_map, rotation_fit_zoom,
                         transition_mask_indices, unit_point, validate_video, write_video)


COMMANDS = ['reframe', 'roll', 'whip', 'wipe', 'match-zoom', 'speed-ramp',
            'subject-track', 'layered-vertigo', 'parallax', 'convert-hdr']


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subs = p.add_subparsers(dest='command', required=True)
    for name in COMMANDS:
        s = subs.add_parser(name, help=name.replace('-', ' '))
        s.add_argument('--output', type=Path, required=True)
        s.add_argument('--width', type=int, default=1280)
        s.add_argument('--height', type=int, default=720)
        s.add_argument('--fps', type=float, default=30)
        s.add_argument('--duration', type=float, default=4., help='Total output seconds; rounded to a whole frame count.')
        s.add_argument('--ffmpeg', default=shutil.which('ffmpeg'))
        s.add_argument('--crf', type=int, default=20)
        s.add_argument('--overwrite', action='store_true')
        s.add_argument('--report', type=Path, help='Default: output filename with .json extension.')
        s.add_argument('--audio', choices=['none', 'source'], default='none',
                       help='source is allowed only for reframe/roll/subject-track/convert-hdr; none writes a silent video.')
        s.add_argument('--bounds', choices=['error', 'clamp'], default='error',
                       help='Reject out-of-source sampling, or explicitly clamp crop centers and record adjustments.')
        s.add_argument('--max-upscale', type=float, default=8., help='Maximum output pixels per source pixel, reported even below this limit.')
        s.add_argument('--easing', choices=['smooth', 'linear'], default='smooth')
        if name not in ['layered-vertigo', 'parallax']:
            s.add_argument('--input', type=Path, required=True)
            s.add_argument('--start', type=float, default=0., help='Source start in seconds.')
        if name in ['reframe', 'roll']:
            s.add_argument('--zoom-start', type=float, default=1.05,
                           help='Crop magnification relative to the largest source crop matching output aspect; 1.0 is that full crop.')
            s.add_argument('--zoom-end', type=float, default=1.3 if name == 'reframe' else 1.05,
                           help='Ending crop magnification, using the same relative units as zoom-start.')
            s.add_argument('--center-start', type=float, nargs=2, default=[.5, .5],
                           help='Initial crop center X Y, normalized to source dimensions in [0,1].')
            s.add_argument('--center-end', type=float, nargs=2, default=[.5, .5],
                           help='Ending crop center X Y, normalized to source dimensions in [0,1].')
        if name == 'roll':
            s.add_argument('--angle-start', type=float, default=0., help='Initial camera roll angle in degrees.')
            s.add_argument('--angle-end', type=float, default=360., help='Ending roll angle in degrees; 360 makes one complete rotation.')
            s.add_argument('--no-auto-fit', action='store_true', help='Disable automatic safe crop for the entire angle/center range.')
        if name in ['whip', 'wipe', 'match-zoom']:
            s.add_argument('--input-b', type=Path, required=True)
            s.add_argument('--start-b', type=float, default=0.)
            s.add_argument('--cut-at', type=float, help='Transition center on the output timeline; default is duration/2.')
            s.add_argument('--transition', type=float, default=.25 if name == 'match-zoom' else .5)
        if name in ['whip', 'wipe']:
            s.add_argument('--direction', choices=['right', 'left', 'up', 'down'], default='right',
                           help='Edge from which the incoming second image enters.')
        if name == 'whip':
            s.add_argument('--blur', type=int, default=61, help='Peak directional blur width in output pixels.')
        if name == 'wipe':
            s.add_argument('--mask-dir', type=Path)
            s.add_argument('--mask-pattern', default='{index:04d}.png')
            s.add_argument('--mask-offset', type=int, default=0)
            s.add_argument('--mask-fps', type=float, help='Mask sampling rate; defaults to output fps.')
            s.add_argument('--feather', type=float, default=4., help='Linear wipe edge width in output pixels; supplied masks keep their own feathering.')
        if name == 'match-zoom':
            s.add_argument('--anchor-a', type=float, nargs=2, default=[.5, .5],
                           help='First-input visual anchor X Y, normalized to its source dimensions in [0,1].')
            s.add_argument('--anchor-b', type=float, nargs=2, default=[.5, .5],
                           help='Second-input visual anchor X Y, normalized to its source dimensions in [0,1].')
            s.add_argument('--base-zoom', type=float, default=1.05, help='Wide-view relative crop magnification.')
            s.add_argument('--peak-zoom', type=float, default=3., help='Transition-peak relative crop magnification.')
        if name == 'speed-ramp':
            s.add_argument('--source-duration', type=float, help='Source span; defaults to all remaining source frames.')
            s.add_argument('--speed-points', default='0:1,0.5:4,1:1',
                           help='fraction:relative-speed pairs, normalized to fit the selected source span into output duration.')
            s.add_argument('--allow-repeat', action='store_true', help='Permit repeated real source frames; interpolation is never used.')
        if name == 'subject-track':
            g = s.add_mutually_exclusive_group(required=True)
            g.add_argument('--roi', type=float, nargs=4, metavar=('X', 'Y', 'WIDTH', 'HEIGHT'),
                           help='Normalized initial tracking box. Select the subject, avoiding background.')
            g.add_argument('--anchor', type=float, nargs=2, help='Normalized point; initializes a small texture ROI around it.')
            s.add_argument('--zoom', type=float, default=1.4)
            s.add_argument('--target', type=float, nargs=2, default=[.5, .5],
                           help='Desired subject position X Y, normalized to output dimensions in [0,1].')
            s.add_argument('--min-confidence', type=float, default=.5)
            s.add_argument('--fallback', choices=['stop', 'hold'], default='stop')
            s.add_argument('--smoothing', type=float, default=.25, help='Crop-center smoothing time constant in seconds; zero disables it.')
        if name in ['layered-vertigo', 'parallax']:
            s.add_argument('--foreground', type=Path, required=True)
            s.add_argument('--background', type=Path, required=True)
            s.add_argument('--alpha', type=Path, required=True, help='White foreground, black background; same size as foreground.')
            s.add_argument('--anchor', type=float, nargs=2, default=[.5, .5],
                           help='Scaling anchor X Y, normalized to the corresponding input/output dimensions in [0,1].')
            for layer in ['foreground', 'background']:
                s.add_argument(f'--{layer}-scale-start', type=float, default=1.2,
                               help='Initial relative layer magnification; 1 covers the output while preserving source aspect.')
                s.add_argument(f'--{layer}-scale-end', type=float, default=1.2 if layer == 'foreground' else 1.05,
                               help='Ending relative layer magnification, in the same units as scale-start.')
                s.add_argument(f'--{layer}-shift-start', type=float, nargs=2, default=[0., 0.],
                               help='Initial X Y translation as fractions of output width/height; negative values allowed.')
                s.add_argument(f'--{layer}-shift-end', type=float, nargs=2, default=[0., 0.],
                               help='Ending X Y translation as fractions of output width/height; negative values allowed.')
        if name == 'convert-hdr':
            s.add_argument('--peak', type=float, default=10., help='Hable tonemap source peak relative to SDR white.')
    return p


def validate_args(a):
    if not a.ffmpeg or not Path(a.ffmpeg).is_file():
        raise MotionError('Supply --ffmpeg or place an FFmpeg executable on PATH.')
    numbers = [a.fps, a.duration, a.max_upscale]
    if not np.isfinite(numbers).all() or min(numbers) <= 0:
        raise MotionError('fps, duration and max-upscale must be finite and positive.')
    if a.width < 2 or a.height < 2 or a.width % 2 or a.height % 2:
        raise MotionError('Output dimensions must be positive even numbers.')
    if not 0 <= a.crf <= 51:
        raise MotionError('CRF must be in [0, 51].')
    if a.output.suffix.lower() != '.mp4':
        raise MotionError('Output must have an .mp4 extension.')
    a.count = int(round(a.fps*a.duration))
    if a.count < 2:
        raise MotionError('Request at least two output frames.')
    a.duration = a.count/a.fps
    a.size = (a.width, a.height)
    a.report = a.report or a.output.with_suffix('.json')
    if a.report.resolve() == a.output.resolve():
        raise MotionError('The JSON report cannot overwrite the video.')
    if not a.overwrite and (a.output.exists() or a.report.exists()):
        raise MotionError('Output/report already exists; use --overwrite to replace it.')
    for key in ['input', 'input_b', 'foreground', 'background', 'alpha']:
        path = getattr(a, key, None)
        if path and path.resolve() in [a.output.resolve(), a.report.resolve()]:
            raise MotionError('Output and report must not overwrite an input asset.')
    if a.audio == 'source' and a.command not in ['reframe', 'roll', 'subject-track', 'convert-hdr']:
        raise MotionError('Source audio is unsupported for retimed, two-scene, or layered commands. Use --audio none and mix sound deliberately.')
    if hasattr(a, 'start') and (not np.isfinite(a.start) or a.start < 0):
        raise MotionError('Source start must be finite and nonnegative.')
    if hasattr(a, 'transition'):
        a.cut_at = a.duration/2 if a.cut_at is None else a.cut_at
        if not np.isfinite([a.cut_at, a.transition, a.start_b]).all() or a.transition <= 0 or a.start_b < 0:
            raise MotionError('Transition must be positive, start-b nonnegative, and times finite.')
        a.cut_start, a.cut_end = a.cut_at-a.transition/2, a.cut_at+a.transition/2
        if a.cut_start < 0 or a.cut_end > a.duration:
            raise MotionError('The entire transition must fit inside the output duration.')


def source_mux(a, picture, final):
    if a.audio == 'none':
        shutil.copyfile(picture, final)
        return
    if not media_metadata(a.ffmpeg, a.input)['audio']:
        raise MotionError('The requested source has no readable audio track; use --audio none.')
    cmd = [a.ffmpeg, '-v', 'error', '-y', '-i', str(picture), '-ss', str(a.start),
           '-t', str(a.duration), '-i', str(a.input), '-map', '0:v:0', '-map', '1:a:0',
           '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-t', str(a.duration),
           '-map_metadata', '-1', '-movflags', '+faststart', str(final)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode:
        raise MotionError(result.stderr[-4000:])


def ordinary_frames(a, readers, report):
    camera = Camera(a.size, a.bounds, a.max_upscale)
    report['geometry'] = camera
    src = readers[0]
    if a.command in ['reframe', 'roll']:
        src.check_range(a.start, a.start+a.duration)
        unit_point(a.center_start); unit_point(a.center_end)
        if min(a.zoom_start, a.zoom_end) <= 0:
            raise MotionError('Zoom values must be positive.')
        safe_zoom = 0.
        if a.command == 'roll' and not a.no_auto_fit:
            safe_zoom = max(rotation_fit_zoom((src.width, src.height), a.size,
                float(mix(a.angle_start, a.angle_end, ease(i/(a.count-1), a.easing))),
                mix(a.center_start, a.center_end, ease(i/(a.count-1), a.easing))) for i in range(a.count))
            report['automatic_constant_zoom_floor'] = safe_zoom
        for i in range(a.count):
            u = ease(i/(a.count-1), a.easing)
            angle = float(mix(a.angle_start, a.angle_end, u)) if a.command == 'roll' else 0.
            yield camera.draw(src.at(a.start+i/a.fps), max(safe_zoom, float(mix(a.zoom_start, a.zoom_end, u))),
                              mix(a.center_start, a.center_end, u), angle)
        return
    if a.command == 'speed-ramp':
        if src.still:
            raise MotionError('A speed ramp requires a video.')
        span = a.source_duration if a.source_duration is not None else src.duration-a.start
        src.check_range(a.start, a.start+span)
        try:
            points = [[float(v) for v in item.split(':')] for item in a.speed_points.split(',')]
        except ValueError as exc:
            raise MotionError('Use speed points such as 0:1,0.5:4,1:1.') from exc
        positions, speeds = normalized_speed_map(a.count, span-1/src.fps, points)
        indices = np.floor((a.start+positions)*src.fps+1e-7).astype(int)
        repeated = int(np.count_nonzero(np.diff(indices) == 0))
        if repeated and not a.allow_repeat:
            raise MotionError(f'Time mapping repeats {repeated} source frames. Shorten duration, use higher-fps input, or explicitly permit --allow-repeat.')
        report['time_mapping'] = dict(source_frames=indices.tolist(), repeated_source_frames=repeated,
            dropped_source_frames=int(np.maximum(0, np.diff(indices)-1).sum()), interpolation=False,
            relative_speed_points=points, actual_speed_range=[float(speeds.min()*a.fps), float(speeds.max()*a.fps)])
        for index in indices:
            yield camera.draw(src.frame_index(index))
        return
    if a.command == 'subject-track':
        yield from tracking_frames(a, src, camera, report)
        return
    second = readers[1]
    src.check_range(a.start, a.start+a.cut_end)
    second.check_range(a.start_b, a.start_b+a.duration-a.cut_start)
    if a.command == 'match-zoom':
        unit_point(a.anchor_a); unit_point(a.anchor_b)
        if min(a.base_zoom, a.peak_zoom) <= 0:
            raise MotionError('Match-zoom magnifications must be positive.')
    mask_reader = None
    if a.command == 'wipe' and a.mask_dir:
        a.mask_fps = a.mask_fps or a.fps
        if not np.isfinite(a.mask_fps) or a.mask_fps <= 0 or a.mask_offset < 0:
            raise MotionError('Mask fps must be positive and mask offset nonnegative.')
        indices = transition_mask_indices(a.count, a.fps, a.cut_start, a.cut_end, a.mask_fps, a.mask_offset)
        if not indices:
            raise MotionError('Transition is shorter than an output frame.')
        for index in sorted(set(indices)):
            mask_path(a.mask_dir, a.mask_pattern, index)
        first = cv2.imread(str(mask_path(a.mask_dir, a.mask_pattern, indices[0])), 0)
        last = cv2.imread(str(mask_path(a.mask_dir, a.mask_pattern, indices[-1])), 0)
        if first is None or last is None or first.mean()/255 > .02 or last.mean()/255 < .98:
            raise MotionError('Tracked masks must begin mostly black and end mostly white; adjust the transition/mask timing.')
        report['wipe'] = dict(type='supplied-mask composite; tracking was performed by the caller',
                              mask_indices=indices, mask_white='second input')
        mask_reader = True
    elif a.command == 'wipe':
        if not np.isfinite(a.feather) or a.feather <= 0:
            raise MotionError('Feather must be finite and positive.')
        report['wipe'] = dict(type='linear digital wipe; not tracked physical occlusion')
    for i in range(a.count):
        t = i/a.fps
        if a.command == 'match-zoom':
            if t < a.cut_start-1e-7:
                u = ease(t/max(a.cut_at, 1e-8), a.easing)
                yield camera.draw(src.at(a.start+t), float(mix(a.base_zoom, a.peak_zoom, u**3)),
                                  mix([.5, .5], a.anchor_a, u**2))
            elif t >= a.cut_end-1e-7:
                u = ease((t-a.cut_at)/max(a.duration-a.cut_at, 1e-8), a.easing)
                yield camera.draw(second.at(a.start_b+max(0., t-a.cut_start)), float(mix(a.peak_zoom, a.base_zoom, u)),
                                  mix(a.anchor_b, [.5, .5], u))
            else:
                x = camera.draw(src.at(a.start+t), a.peak_zoom, a.anchor_a)
                y = camera.draw(second.at(a.start_b+max(0., t-a.cut_start)), a.peak_zoom, a.anchor_b)
                u = ease((t-a.cut_start)/a.transition, a.easing)
                yield cv2.GaussianBlur(cv2.addWeighted(x, 1-u, y, u, 0), (0, 0), 1.5)
            continue
        if t < a.cut_start-1e-7:
            yield camera.draw(src.at(a.start+t))
            continue
        if t >= a.cut_end-1e-7:
            yield camera.draw(second.at(a.start_b+max(0., t-a.cut_start)))
            continue
        x = camera.draw(src.at(a.start+t))
        y = camera.draw(second.at(a.start_b+max(0., t-a.cut_start)))
        u = ease((t-a.cut_start)/a.transition, a.easing)
        if a.command == 'whip':
            if a.blur < 1:
                raise MotionError('Blur width must be at least one pixel.')
            if a.direction in ['left', 'right']:
                offset = round(a.width*u)
                joined = np.concatenate([x, y] if a.direction == 'right' else [y, x], axis=1)
                start = offset if a.direction == 'right' else a.width-offset
                frame = joined[:, start:start+a.width].copy()
                kernel = (1+2*round((a.blur-1)/2*math.sin(math.pi*u)), 1)
            else:
                offset = round(a.height*u)
                joined = np.concatenate([x, y] if a.direction == 'down' else [y, x], axis=0)
                start = offset if a.direction == 'down' else a.height-offset
                frame = joined[start:start+a.height].copy()
                kernel = (1, 1+2*round((a.blur-1)/2*math.sin(math.pi*u)))
            yield cv2.blur(frame, kernel)
        else:
            if mask_reader:
                index = a.mask_offset+int(math.floor((t-a.cut_start)*a.mask_fps+1e-7))
                mask = cv2.imread(str(mask_path(a.mask_dir, a.mask_pattern, index)), 0)
                if mask is None or abs(mask.shape[1]/mask.shape[0]-src.width/src.height) > .002:
                    raise MotionError('Mask aspect must match its source video. Mask geometry is not automatically inferred.')
                mask = cv2.resize(mask, (src.width, src.height), interpolation=cv2.INTER_LINEAR)
                matrix, _ = crop_matrix((src.width, src.height), a.size)
                alpha = cv2.warpAffine(mask, matrix, a.size, flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)/255.
            else:
                if a.direction in ['left', 'right']:
                    coord = np.linspace(0, 1, a.width)[None, :]
                    if a.direction == 'right': coord = 1-coord
                    alpha = np.repeat(np.clip((u-coord)*a.width/a.feather+.5, 0, 1), a.height, axis=0)
                else:
                    coord = np.linspace(0, 1, a.height)[:, None]
                    if a.direction == 'down': coord = 1-coord
                    alpha = np.repeat(np.clip((u-coord)*a.height/a.feather+.5, 0, 1), a.width, axis=1)
            yield np.uint8(x*(1-alpha[:, :, None])+y*alpha[:, :, None]+.5)


def tracking_frames(a, src, camera, report):
    src.check_range(a.start, a.start+a.duration)
    target = unit_point(a.target, 'target')
    if a.zoom <= 0 or not 0 < a.min_confidence <= 1 or a.smoothing < 0:
        raise MotionError('Tracking zoom must be positive; confidence in (0,1]; smoothing nonnegative.')
    if a.roi is None:
        anchor = unit_point(a.anchor)
        x, y = np.clip(anchor-.08, 0, .84)
        a.roi = [x, y, .16, .16]
    x, y, width, height = a.roi
    if not np.isfinite(a.roi).all() or min(x, y) < 0 or min(width, height) <= 0 or x+width > 1 or y+height > 1:
        raise MotionError('ROI must be a nonempty normalized box entirely inside the source.')
    box = np.array([[x, y], [x+width, y], [x+width, y+height], [x, y+height]])*np.array([src.width-1, src.height-1])
    previous = None; points = None; smoothed = None; holding = False; held_center = None
    records = []
    for i in range(a.count):
        frame = src.at(a.start+i/a.fps)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        confidence = 1.
        if previous is not None and not holding:
            q, status, error = cv2.calcOpticalFlowPyrLK(previous, gray, points, None, winSize=(25, 25), maxLevel=3)
            back, back_status, _ = cv2.calcOpticalFlowPyrLK(gray, previous, q, None, winSize=(25, 25), maxLevel=3)
            good = ((status.ravel() > 0) & (back_status.ravel() > 0) & (error.ravel() < 25)
                    & (np.linalg.norm(back-points, axis=2).ravel() < 1.))
            affine, flags = (None, None)
            if good.sum() >= 6:
                affine, flags = cv2.estimateAffinePartial2D(points[good], q[good], method=cv2.RANSAC, ransacReprojThreshold=2.)
            confidence = 0. if affine is None else float(flags.sum()/len(points))
            new_box = box if affine is None else cv2.transform(box.astype(np.float32)[None], affine)[0]
            reliable = (confidence >= a.min_confidence and np.isfinite(new_box).all()
                        and np.min(new_box) >= 0 and np.max(new_box[:, 0]) < src.width
                        and np.max(new_box[:, 1]) < src.height)
            if not reliable:
                if a.fallback == 'stop':
                    raise MotionError(f'Tracking lost at output frame {i} (confidence {confidence:.3f}); no semantic identity is inferred.')
                holding = True
            else:
                box = new_box
        if holding:
            center = held_center
        else:
            lo, hi = box.min(axis=0), box.max(axis=0)
            location = (lo+hi)/2
            alpha = 1. if a.smoothing == 0 else 1-math.exp(-1/(a.fps*a.smoothing))
            smoothed = location if smoothed is None else mix(smoothed, location, alpha)
            step = min(src.width/a.width, src.height/a.height)/a.zoom
            half = np.array([(a.width-1)*step, (a.height-1)*step])/2
            if np.any(hi-lo > 2*half):
                raise MotionError('The tracked ROI is larger than the crop; reduce zoom.')
            pixel_center = smoothed-(target-.5)*np.array([a.width, a.height])*step
            pixel_center = np.clip(pixel_center, hi-half, lo+half)
            center = pixel_center/np.array([src.width-1, src.height-1])
            center = unit_point(center, 'tracked crop center')
            held_center = center.copy()
        result = camera.draw(frame, a.zoom, center)
        if not holding:
            actual = np.array(camera.diagnostics[-1]['actual_center'])*np.array([src.width-1, src.height-1])
            step = min(src.width/a.width, src.height/a.height)/a.zoom
            out_box = (box-actual)/step+np.array([(a.width-1)/2, (a.height-1)/2])
            if (out_box.min() < -.1 or out_box[:, 0].max() > a.width-.9 or out_box[:, 1].max() > a.height-.9):
                raise MotionError('The tracked ROI cannot be retained inside the selected framing.')
            mask = np.zeros_like(gray)
            cv2.fillConvexPoly(mask, box.astype(np.int32), 255)
            points = cv2.goodFeaturesToTrack(gray, 150, .01, 5, mask=mask)
            if points is None or len(points) < 6:
                if a.fallback == 'stop':
                    raise MotionError(f'Insufficient texture in the tracking ROI at frame {i}.')
                holding = True
        records.append(dict(frame=i, confidence=confidence, held_after_loss=holding,
                            box_normalized=(box/np.array([src.width-1, src.height-1])).tolist()))
        previous = gray
        yield result
    report['tracking'] = dict(method='ROI texture LK forward/backward + RANSAC; no semantic object identification',
                              fallback=a.fallback, frames=records)


def layered_frames(a, report):
    foreground = cv2.imread(str(a.foreground), cv2.IMREAD_UNCHANGED)
    background = cv2.imread(str(a.background), cv2.IMREAD_UNCHANGED)
    alpha = cv2.imread(str(a.alpha), cv2.IMREAD_GRAYSCALE)
    if any(x is None for x in [foreground, background, alpha]):
        raise MotionError('Foreground/background/alpha must be readable images.')
    if any(x.dtype != np.uint8 for x in [foreground, background]) or foreground.ndim != 3 or background.ndim != 3:
        raise MotionError('Layer RGB images must be 8-bit SDR.')
    foreground, background = foreground[:, :, :3], background[:, :, :3]
    if alpha.shape != foreground.shape[:2]:
        raise MotionError('Alpha must have exactly the same dimensions as foreground.')
    anchor = unit_point(a.anchor)
    margins = []; near_scales = []; far_scales = []
    def matrix(image, scale, shift, require_coverage):
        if not np.isfinite([scale, *shift]).all() or scale <= 0:
            raise MotionError('Layer magnifications must be finite positive; shifts finite.')
        h, w = image.shape[:2]
        factor = max(a.width/w, a.height/h)*scale
        if factor > a.max_upscale:
            raise MotionError('Layer enlargement exceeds --max-upscale.')
        translation = anchor*np.array([a.width-1, a.height-1])+np.asarray(shift)*np.array([a.width, a.height])-factor*anchor*np.array([w-1, h-1])
        transform = np.array([[factor, 0., translation[0]], [0., factor, translation[1]]], np.float32)
        if require_coverage:
            inverse = cv2.invertAffineTransform(transform)
            corners = cv2.transform(np.array([[[0., 0.], [a.width-1., 0.], [a.width-1., a.height-1.], [0., a.height-1.]]]), inverse)[0]
            lo, hi = corners.min(axis=0), corners.max(axis=0)
            if np.any(hi-lo > np.array([w-1, h-1])+1e-4):
                raise MotionError('Background scale is too small to cover the output.')
            correction = np.maximum(0., -lo)+np.minimum(0., np.array([w-1, h-1])-hi)
            if np.max(abs(correction)) > 1e-4:
                if a.bounds == 'error':
                    raise MotionError('Background layer reveals an empty edge; reduce shift, increase scale, or use --bounds clamp.')
                transform[:, 2] -= factor*correction
                corners += correction
            margin = float(min(corners[:, 0].min(), corners[:, 1].min(), w-1-corners[:, 0].max(), h-1-corners[:, 1].max()))
            margins.append(max(0., margin))
        return transform
    for i in range(a.count):
        u = ease(i/(a.count-1), a.easing)
        fs = float(mix(a.foreground_scale_start, a.foreground_scale_end, u))
        bs = float(mix(a.background_scale_start, a.background_scale_end, u))
        fm = matrix(foreground, fs, mix(a.foreground_shift_start, a.foreground_shift_end, u), False)
        bm = matrix(background, bs, mix(a.background_shift_start, a.background_shift_end, u), True)
        near = cv2.warpAffine(foreground, fm, a.size, flags=cv2.INTER_CUBIC)
        far = cv2.warpAffine(background, bm, a.size, flags=cv2.INTER_CUBIC)
        mask = cv2.warpAffine(alpha.astype(np.float32)/255, fm, a.size, flags=cv2.INTER_LINEAR)[:, :, None]
        near_scales.append(fs); far_scales.append(bs)
        yield np.uint8(np.clip(near*mask+far*(1-mask), 0, 255)+.5)
    report['layers'] = dict(method='Rigid two-layer 2.5D composite; no inferred hidden sides or true camera orbit',
        minimum_background_source_margin=min(margins), foreground_scale_range=[min(near_scales), max(near_scales)],
        background_scale_range=[min(far_scales), max(far_scales)], alpha='provided by caller; no automatic background completion')


def convert_hdr(a, final):
    meta = media_metadata(a.ffmpeg, a.input)
    if not meta['hdr']:
        raise MotionError('Input is not tagged HDR; conversion was not applied to avoid double tone-mapping.')
    if not np.isfinite(a.peak) or a.peak <= 0:
        raise MotionError('HDR peak must be positive.')
    vf = (f'fps={a.fps},scale={a.width}:{a.height}:force_original_aspect_ratio=increase,'
          f'crop={a.width}:{a.height},zscale=transfer=linear:npl=100,format=gbrpf32le,'
          f'zscale=primaries=bt709,tonemap=hable:desat=0:peak={a.peak},'
          'zscale=transfer=bt709:matrix=bt709:range=tv,format=yuv420p,setsar=1')
    cmd = [a.ffmpeg, '-v', 'error', '-y', '-ss', str(a.start), '-t', str(a.duration), '-i', str(a.input),
           '-map', '0:v:0', '-vf', vf, '-frames:v', str(a.count), '-c:v', 'libx264', '-preset', 'fast',
           '-crf', str(a.crf), '-threads', '2', '-color_primaries', 'bt709', '-color_trc', 'bt709',
           '-colorspace', 'bt709', '-color_range', 'tv', '-map_metadata', '-1']
    if a.audio == 'source':
        if not meta['audio']:
            raise MotionError('Input has no audio; use --audio none.')
        cmd += ['-map', '0:a:0', '-c:a', 'aac', '-b:a', '192k']
    else: cmd += ['-an']
    cmd += ['-movflags', '+faststart', str(final)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode:
        raise MotionError(result.stderr[-4000:])


def run(a):
    validate_args(a)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.report.parent.mkdir(parents=True, exist_ok=True)
    report = dict(command=a.command, audio='omitted; silent output' if a.audio == 'none' else 'source selection, unchanged time; CFR timing assumed, VFR synchronization not guaranteed',
                  color='SDR BT.709; no tone mapping applied' if a.command != 'convert-hdr' else 'Explicit Hable HDR to SDR conversion',
                  timeline='CFR frame-index selection using source decoder fps; no optical-flow interpolation',
                  parameters={key: str(value) if isinstance(value, Path) else value for key, value in vars(a).items()
                              if key not in ['ffmpeg', 'report']})
    readers = []
    with tempfile.TemporaryDirectory(prefix='.camera-motion-', dir=str(a.output.parent)) as temp:
        work = Path(temp); final = work/'final.mp4'
        try:
            if a.command == 'convert-hdr':
                convert_hdr(a, final)
            else:
                if a.command in ['layered-vertigo', 'parallax']:
                    if a.audio != 'none': raise MotionError('Layered effects have no source audio timeline.')
                    frames = layered_frames(a, report)
                else:
                    readers.append(Reader(a.input, a.ffmpeg))
                    if hasattr(a, 'input_b'):
                        readers.append(Reader(a.input_b, a.ffmpeg))
                    frames = ordinary_frames(a, readers, report)
                picture = work/'picture.mp4'
                write_video(a.ffmpeg, picture, frames, a.size, a.fps, a.count, a.crf)
                source_mux(a, picture, final)
                if isinstance(report.get('geometry'), Camera):
                    report['geometry'] = report['geometry'].summary()
            report['validation'] = validate_video(a.ffmpeg, final, a.size, a.count, a.fps)
            report['source_fps'] = [reader.fps for reader in readers]
            text = json.dumps(report, ensure_ascii=False, indent=2)
            report_tmp = work/'report.json'; report_tmp.write_text(text+'\n')
            os.replace(final, a.output)
            # Report can live on another filesystem; copy next to it before atomic replacement.
            with tempfile.NamedTemporaryFile(dir=str(a.report.parent), prefix='.camera-report-', suffix='.json', delete=False) as f:
                destination_temp = Path(f.name); f.write((text+'\n').encode())
            os.replace(destination_temp, a.report)
        finally:
            for reader in readers: reader.close()
    print(json.dumps(dict(output=str(a.output), report=str(a.report), **report['validation']), ensure_ascii=False))


def main():
    p = parser(); args = p.parse_args()
    try:
        run(args)
    except (MotionError, OSError, cv2.error) as exc:
        p.exit(2, f'camera motion: {exc}\n')


if __name__ == '__main__':
    main()
