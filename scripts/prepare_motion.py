#!/usr/bin/env python3
"""Classical CV preparation: dominant-motion diagnosis, bounded stabilization, static GrabCut masks.

Video timing uses CFR frame indices; VFR is not detected. Inputs must be 8-bit
SDR (use apply_camera_motion.py convert-hdr explicitly for tagged HDR). A fitted
background similarity is a geometric hypothesis, not semantic proof of a static
scene. Stabilized exports are silent. Static masks are not propagated through
video, and no learned segmentation, depth estimation, or 3D reconstruction runs.
"""
from pathlib import Path
from types import SimpleNamespace
from contextlib import ExitStack
import argparse
import json
import math
import os
import shutil
import sys
import tempfile

import cv2
import numpy as np
from scipy.ndimage import gaussian_filter1d

from motion_core import MotionError, Reader, crop_matrix, unit_point, validate_video, write_video


class PreparationRejected(MotionError):
    def __init__(self, reason, details):
        super().__init__(reason)
        self.details = details


def roi_pixels(roi, size):
    w, h = size
    if roi is None:
        return (0, 0, w, h)
    if (len(roi) != 4 or not np.isfinite(roi).all() or min(roi[:2]) < 0
            or min(roi[2:]) <= 0 or roi[0]+roi[2] > 1 or roi[1]+roi[3] > 1):
        raise MotionError('ROI requires normalized X Y WIDTH HEIGHT inside [0,1].')
    x0, y0 = np.floor(np.array(roi[:2])*[w, h]).astype(int)
    x1, y1 = np.ceil((np.array(roi[:2])+roi[2:])*[w, h]).astype(int)
    if x1-x0 < 4 or y1-y0 < 4:
        raise MotionError('The selected ROI is too small at analysis resolution.')
    return int(x0), int(y0), int(x1-x0), int(y1-y0)


def fit_pair(previous, current, mask, settings):
    """Shi–Tomasi -> pyramidal LK -> forward/back checks -> robust similarity."""
    result = dict(accepted=False, reason='insufficient_texture', detected_points=0,
                  forward_backward_points=0, inliers=0, inlier_ratio=0.,
                  coverage_ratio=0., median_reprojection_error=None)
    identity = np.eye(3)
    points = cv2.goodFeaturesToTrack(previous, maxCorners=settings.max_features,
                                   qualityLevel=.01, minDistance=7, mask=mask, blockSize=5)
    if points is None:
        return identity, result
    result['detected_points'] = len(points)
    if len(points) < settings.min_points:
        return identity, result
    nxt, status, error = cv2.calcOpticalFlowPyrLK(previous, current, points, None,
        winSize=(21, 21), maxLevel=3,
        criteria=(cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 30, .01))
    if nxt is None:
        result['reason'] = 'optical_flow_failed'
        return identity, result
    back, reverse_status, _ = cv2.calcOpticalFlowPyrLK(current, previous, nxt, None,
        winSize=(21, 21), maxLevel=3)
    if back is None:
        result['reason'] = 'reverse_flow_failed'
        return identity, result
    good = ((status.ravel() > 0) & (reverse_status.ravel() > 0)
            & (error.ravel() < 25) & np.isfinite(nxt).all(axis=(1, 2))
            & (np.linalg.norm(back-points, axis=2).ravel() <= settings.fb_threshold))
    a, b = points[good].reshape(-1, 2), nxt[good].reshape(-1, 2)
    result['forward_backward_points'] = len(a)
    if len(a) < settings.min_points:
        result['reason'] = 'insufficient_consistent_flow'
        return identity, result
    affine, flags = cv2.estimateAffinePartial2D(a, b, method=cv2.RANSAC,
        ransacReprojThreshold=settings.ransac_threshold, maxIters=2000,
        confidence=.995, refineIters=10)
    if affine is None or not np.isfinite(affine).all():
        result['reason'] = 'similarity_fit_failed'
        return identity, result
    inside = flags.ravel().astype(bool)
    result['inliers'] = int(inside.sum())
    result['inlier_ratio'] = float(inside.mean())
    predicted = cv2.transform(a[None], affine)[0]
    result['median_reprojection_error'] = float(np.median(np.linalg.norm(predicted[inside]-b[inside], axis=1))) if inside.any() else None
    if inside.sum() >= 3:
        hull = cv2.convexHull(a[inside].astype(np.float32))
        result['coverage_ratio'] = float(cv2.contourArea(hull)/max(1, cv2.countNonZero(mask)))
    scale = float(np.hypot(affine[0, 0], affine[1, 0]))
    rotation = float(np.degrees(np.arctan2(affine[1, 0], affine[0, 0])))
    center = np.array([(previous.shape[1]-1)/2, (previous.shape[0]-1)/2])
    displacement = affine[:, :2]@center+affine[:, 2]-center
    result.update(scale_ratio=scale, rotation_degrees=rotation,
                  center_displacement_analysis_pixels=displacement.tolist())
    rejected = []
    if inside.sum() < settings.min_points: rejected.append('too_few_inliers')
    if result['inlier_ratio'] < settings.min_inlier_ratio: rejected.append('low_inlier_ratio')
    if result['coverage_ratio'] < settings.min_coverage: rejected.append('localized_feature_support')
    if not .9 <= scale <= 1.1: rejected.append('abrupt_scale_change')
    if abs(rotation) > 15: rejected.append('abrupt_rotation')
    if np.linalg.norm(displacement) > .35*np.hypot(*previous.shape): rejected.append('abrupt_translation')
    result['accepted'] = not rejected
    result['reason'] = 'model_fit_passed' if not rejected else ','.join(rejected)
    result['incremental_similarity'] = affine.tolist()
    if rejected:
        return identity, result
    full = np.eye(3); full[:2] = affine
    return full, result


def analyze_frames(frames, settings):
    """Stream images, retaining transforms and scalar diagnostics rather than frames."""
    poses, records, previous = [np.eye(3)], [], None
    source_size = analysis_size = None
    mask = None
    count, initial_features = 0, 0
    for i, image in enumerate(frames):
        h, w = image.shape[:2]
        if source_size is None:
            source_size = (w, h)
            factor = min(1., settings.analysis_width/w)
            analysis_size = (max(16, round(w*factor)), max(16, round(h*factor)))
            x, y, rw, rh = roi_pixels(settings.background_roi, analysis_size)
            mask = np.zeros((analysis_size[1], analysis_size[0]), np.uint8)
            mask[y:y+rh, x:x+rw] = 255
        elif source_size != (w, h):
            raise MotionError('Source dimensions changed within the selected interval.')
        gray = cv2.cvtColor(cv2.resize(image, analysis_size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        if previous is None:
            features = cv2.goodFeaturesToTrack(gray, settings.max_features, .01, 7, mask=mask)
            initial_features = 0 if features is None else len(features)
        if previous is not None:
            matrix, record = fit_pair(previous, gray, mask, settings)
            record['frame'] = i
            records.append(record)
            # Invalid pairs are explicitly marked. Holding them here is only a
            # placeholder; stabilization rejects them unless --fallback hold.
            poses.append(matrix@poses[-1])
        previous, count = gray, i+1
    if not count:
        raise MotionError('The input contained no selected frames.')
    return dict(poses=np.stack(poses), records=records, source_size=source_size,
                analysis_size=analysis_size, count=count,
                initial_detected_features=initial_features)


def pose_path(poses, analysis_size):
    center = np.array([(analysis_size[0]-1)/2, (analysis_size[1]-1)/2, 1.])
    xy = (poses@center)[:, :2]
    angle = np.unwrap(np.arctan2(poses[:, 1, 0], poses[:, 0, 0]))
    scale = np.log(np.hypot(poses[:, 0, 0], poses[:, 1, 0]))
    return np.column_stack([xy, angle, scale])


def smooth_path(path, sigma_frames):
    if sigma_frames <= 0 or len(path) < 3:
        return path.copy()
    pad = max(4, int(math.ceil(4*sigma_frames)))
    count = min(12, len(path))
    columns = []
    for column in path.T:
        left = np.polyfit(np.arange(count), column[:count], 1)[0]
        right = np.polyfit(np.arange(count), column[-count:], 1)[0]
        extended = np.r_[column[0]+left*np.arange(-pad, 0), column,
                         column[-1]+right*np.arange(1, pad+1)]
        columns.append(gaussian_filter1d(extended, sigma_frames, mode='nearest')[pad:-pad])
    return np.column_stack(columns)


def residual_metrics(path, reference):
    error = path-reference
    return dict(translation_rms_analysis_pixels=float(np.sqrt(np.mean(np.sum(error[:, :2]**2, axis=1)))),
                rotation_rms_degrees=float(np.degrees(np.sqrt(np.mean(error[:, 2]**2)))),
                log_scale_rms_percent=float(100*np.sqrt(np.mean(error[:, 3]**2))))


def rejected_intervals(records, fps, start=0.):
    frames = [r['frame'] for r in records if not r['accepted']]
    groups = []
    for frame in frames:
        if groups and frame == groups[-1][1]: groups[-1][1] += 1
        else: groups.append([frame, frame+1])
    return [dict(start_frame=a, end_frame_exclusive=b, output_seconds=[a/fps, b/fps],
                 source_seconds_cfr_assumption=[start+a/fps, start+b/fps]) for a, b in groups]


def analysis_report(result, args, reader):
    path = pose_path(result['poses'], result['analysis_size'])
    smooth = smooth_path(path, .15*args.fps)
    records = result['records']
    accepted = sum(row['accepted'] for row in records)
    return dict(command=args.command, method='Shi-Tomasi + sparse pyramidal LK + forward/backward check + RANSAC four-DOF similarity',
        input=dict(name=reader.path.name, dimensions=list(result['source_size']),
                   source_fps=reader.fps, source_frame_count=reader.count,
                   source_duration=None if reader.still else reader.duration,
                   hdr=False, input_audio_present=reader.metadata['audio'], still=reader.still),
        timing=dict(fps=args.fps, frames=result['count'], start=args.start,
                    assumption='CFR frame-index sampling; VFR not detected', interpolation=False),
        support=dict(background_roi=args.background_roi,
                     static_background_semantically_verified=False,
                     warning='A coherent moving subject or flowing surface can fit this model. Select static background and review the result.'),
        analysis_size=list(result['analysis_size']),
        initial_detected_features=result['initial_detected_features'],
        quality=dict(valid_pairs=accepted, total_pairs=len(records),
                     valid_pair_fraction=accepted/len(records) if records else None,
                     rejected_intervals=rejected_intervals(records, args.fps, args.start),
                     min_inlier_ratio=args.min_inlier_ratio, min_coverage=args.min_coverage,
                     minimum_points=args.min_points),
        diagnostics=records, estimated_path=path.tolist(),
        model_high_frequency_residual=residual_metrics(path, smooth),
        interpretation='Model residuals describe a dominant 2D motion fit, not measured 3D camera motion or semantic scene correctness.')


def stabilization_maps(result, size, fps, smoothing, strength, zoom, max_zoom, auto_crop):
    """Compose inverse similarity correction and a single safe crop for every frame."""
    sw, sh = result['source_size']; aw, ah = result['analysis_size']
    path = pose_path(result['poses'], result['analysis_size'])
    reference = smooth_path(path, smoothing*fps)
    target = path+strength*(reference-path)
    center = np.array([(aw-1)/2, (ah-1)/2])
    lift = np.diag([sw/aw, sh/ah, 1.])
    inverse_lift = np.linalg.inv(lift)
    corrections = []
    for i, pose in enumerate(result['poses']):
        angle, scale = target[i, 2], math.exp(target[i, 3])
        linear = scale*np.array([[math.cos(angle), -math.sin(angle)],
                                 [math.sin(angle), math.cos(angle)]])
        desired = np.eye(3); desired[:2, :2] = linear
        desired[:2, 2] = target[i, :2]-linear@center
        corrections.append(lift@desired@np.linalg.inv(pose)@inverse_lift)
    output_corners = np.array([[0, size[0]-1, size[0]-1, 0],
                               [0, 0, size[1]-1, size[1]-1], [1, 1, 1, 1]])
    def at(magnification):
        crop, _ = crop_matrix((sw, sh), size, magnification)
        crop3 = np.vstack([crop, [0, 0, 1]])
        maps, margins, upscales = [], [], []
        for correction in corrections:
            matrix = np.linalg.inv(correction)@crop3
            points = matrix@output_corners
            margins.append(float(min(points[0].min(), points[1].min(),
                                     sw-1-points[0].max(), sh-1-points[1].max())))
            upscales.append(float(1/np.linalg.svd(matrix[:2, :2], compute_uv=False).min()))
            maps.append(matrix[:2].astype(np.float32))
        return maps, min(margins), max(upscales)
    selected = zoom
    maps, margin, upscale = at(selected)
    if margin < 2 and auto_crop:
        _, largest_margin, _ = at(max_zoom)
        if largest_margin < 2:
            raise MotionError(f'No safe crop up to max-zoom={max_zoom}; increase the limit, reduce strength, or shorten the interval.')
        lo, hi = zoom, max_zoom
        for _ in range(28):
            middle = (lo+hi)/2
            if at(middle)[1] >= 2: hi = middle
            else: lo = middle
        selected = hi
        maps, margin, upscale = at(selected)
    if margin < 1.999:
        raise MotionError('Stabilization reveals source edges at the requested crop; enable auto-crop or increase zoom.')
    return maps, dict(zoom=selected, minimum_source_pixel_margin=margin,
        maximum_output_pixels_per_source_pixel=upscale, strength=strength,
        smoothing_seconds=smoothing, model_residual_before=residual_metrics(path, reference),
        predicted_model_residual_after=residual_metrics(target, reference),
        measurement_note='After metrics are predicted from fitted transforms, not an independent encoded-image quality measurement.')


def static_grabcut(source, roi, foreground_points=(), background_points=(),
                   iterations=5, analysis_width=960, feather=0.):
    """User-supervised color segmentation on one image, returned at source size."""
    h, w = source.shape[:2]
    factor = min(1., analysis_width/w)
    aw, ah = max(16, round(w*factor)), max(16, round(h*factor))
    image = cv2.resize(source, (aw, ah), interpolation=cv2.INTER_AREA)
    x, y, rw, rh = roi_pixels(roi, (aw, ah))
    if rw*rh >= aw*ah:
        raise MotionError('GrabCut needs some known background outside the ROI.')
    labels = np.full((ah, aw), cv2.GC_BGD, np.uint8)
    labels[y:y+rh, x:x+rw] = cv2.GC_PR_FGD
    seeds = []
    for points, kind in [(foreground_points, 'foreground'), (background_points, 'background')]:
        seed = np.zeros((ah, aw), np.uint8)
        for point in points:
            nx, ny = unit_point(point, kind+' seed')
            px, py = round(nx*(aw-1)), round(ny*(ah-1))
            if kind == 'foreground' and not (x <= px < x+rw and y <= py < y+rh):
                raise MotionError('Foreground seeds must lie inside the selected ROI.')
            cv2.circle(seed, (px, py), max(1, round(min(aw, ah)*.003)), 255, -1)
        seeds.append(seed)
    if np.any((seeds[0] > 0) & (seeds[1] > 0)):
        raise MotionError('Foreground and background seed regions overlap.')
    labels[(seeds[0] > 0) & (labels == cv2.GC_PR_FGD)] = cv2.GC_FGD
    labels[seeds[1] > 0] = cv2.GC_BGD
    bg, fg = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    cv2.setRNGSeed(0)
    cv2.grabCut(image, labels, None, bg, fg, iterations, cv2.GC_INIT_WITH_MASK)
    binary = np.uint8(np.isin(labels, [cv2.GC_FGD, cv2.GC_PR_FGD]))*255
    if not binary.any() or np.all(binary == 255):
        raise MotionError('GrabCut produced an empty/full mask; adjust ROI or supply clear foreground/background seeds.')
    output = cv2.resize(binary, (w, h), interpolation=cv2.INTER_NEAREST)
    if feather > 0:
        output = cv2.GaussianBlur(output, (0, 0), feather)
    return output, dict(method='Static ROI-constrained GrabCut color segmentation',
        input_size=[w, h], analysis_size=[aw, ah], roi=list(roi),
        foreground_points=[list(x) for x in foreground_points],
        background_points=[list(x) for x in background_points], iterations=iterations,
        foreground_fraction=float(np.mean(binary > 0)), feather_source_pixels=feather,
        alpha_white='foreground', learned_segmentation=False, temporal_propagation=False,
        limitations='Color/texture separation needs user supervision; this is not semantic segmentation or a video mask tracker.')


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subs = parser.add_subparsers(dest='command', required=True)
    for command in ['analyze', 'stabilize', 'mask']:
        p = subs.add_parser(command, help={'analyze':'Diagnose a dominant background similarity model',
                           'stabilize':'Smooth the fitted dominant motion and crop to valid source pixels',
                           'mask':'Segment one still/video frame using user ROI and GrabCut'}[command])
        p.add_argument('--input', type=Path, required=True)
        p.add_argument('--output', type=Path, required=True)
        p.add_argument('--ffmpeg', default=shutil.which('ffmpeg'))
        p.add_argument('--overwrite', action='store_true')
        p.add_argument('--analysis-width', type=int, default=960 if command == 'mask' else 640,
                       help='Maximum analysis-image width in pixels; never enlarges the source.')
        if command != 'analyze': p.add_argument('--report', type=Path, help='Default: output filename with .json extension.')
        if command == 'mask':
            p.add_argument('--roi', type=float, nargs=4, required=True,
                           help='Source-normalized X Y WIDTH HEIGHT rectangle enclosing the foreground.')
            p.add_argument('--time', type=float, default=0., help='Video source time in seconds; a still image uses zero.')
            p.add_argument('--foreground-point', type=float, nargs=2, action='append', default=[],
                           help='Known foreground X Y in source-normalized [0,1] coordinates; repeat for more seeds.')
            p.add_argument('--background-point', type=float, nargs=2, action='append', default=[],
                           help='Known background X Y in source-normalized [0,1] coordinates; repeat for more seeds.')
            p.add_argument('--iterations', type=int, default=5)
            p.add_argument('--feather', type=float, default=0., help='Gaussian feather sigma in final source-size pixels; zero gives a binary mask.')
        else:
            p.add_argument('--start', type=float, default=0., help='CFR source start time in seconds.')
            p.add_argument('--duration', type=float, default=4., help='Selected seconds, rounded to a whole output-frame count.')
            p.add_argument('--fps', type=float, default=30., help='CFR sampling/output frames per second; no interpolation.')
            p.add_argument('--background-roi', type=float, nargs=4,
                           help='Source-normalized X Y WIDTH HEIGHT region containing static background; omitted means full frame, with no semantic guarantee.')
            p.add_argument('--max-features', type=int, default=600)
            p.add_argument('--min-points', type=int, default=12)
            p.add_argument('--min-inlier-ratio', type=float, default=.55)
            p.add_argument('--min-coverage', type=float, default=.08, help='Minimum convex-hull area of inlier features as a fraction of the analysis ROI.')
            p.add_argument('--fb-threshold', type=float, default=1., help='Maximum forward/backward LK error in analysis pixels.')
            p.add_argument('--ransac-threshold', type=float, default=1.5, help='RANSAC inlier reprojection threshold in analysis pixels.')
        if command == 'stabilize':
            p.add_argument('--width', type=int, default=1280)
            p.add_argument('--height', type=int, default=720)
            p.add_argument('--smoothing', type=float, default=.15, help='Gaussian path smoothing sigma in seconds.')
            p.add_argument('--strength', type=float, default=.8, help='Fraction of correction towards the smoothed path, from 0 to 1.')
            p.add_argument('--zoom', type=float, default=1.05, help='Minimum crop magnification relative to the largest aspect-matched crop.')
            p.add_argument('--max-zoom', type=float, default=1.5, help='Maximum uniform auto-crop magnification; no per-frame zoom pumping.')
            p.add_argument('--no-auto-crop', action='store_true')
            p.add_argument('--fallback', choices=['stop', 'hold'], default='stop',
                           help='stop rejects any failed model pair; hold explicitly assumes zero incremental motion there and records the intervals.')
            p.add_argument('--crf', type=int, default=20)
    return parser


def validate_args(a):
    a.ffmpeg = str(a.ffmpeg or '')
    executable = shutil.which(a.ffmpeg)
    if executable: a.ffmpeg = executable
    if not a.ffmpeg or not Path(a.ffmpeg).is_file(): raise MotionError('Supply --ffmpeg or put FFmpeg on PATH.')
    if a.analysis_width < 32: raise MotionError('analysis-width must be at least 32 pixels.')
    expected = {'analyze':'.json', 'stabilize':'.mp4', 'mask':'.png'}[a.command]
    if a.output.suffix.lower() != expected: raise MotionError(f'{a.command} output must have a {expected} extension.')
    a.report = None if a.command == 'analyze' else a.report or a.output.with_suffix('.json')
    targets = [a.output]+([a.report] if a.report else [])
    if len({x.resolve() for x in targets}) != len(targets): raise MotionError('Output and report must have different paths.')
    for path in targets:
        if path.resolve() == a.input.resolve(): raise MotionError('An output cannot replace its input.')
        if path.exists() and path.samefile(a.input): raise MotionError('An output cannot be a hard-link alias of its input.')
        if path.exists() and not path.is_file(): raise MotionError('Output/report targets must be regular files, not directories.')
        if path.is_symlink(): raise MotionError('Output/report symlinks are unsupported; choose an ordinary destination file.')
        if path.exists() and not a.overwrite: raise MotionError('Output/report already exists; use --overwrite to replace it.')
        path.parent.mkdir(parents=True, exist_ok=True)
    if len(targets) == 2 and all(path.exists() for path in targets) and targets[0].samefile(targets[1]):
        raise MotionError('Output and report cannot be hard-link aliases of each other.')
    if a.command == 'mask':
        if not np.isfinite([a.time, a.feather]).all() or min(a.time, a.feather) < 0 or not 1 <= a.iterations <= 50:
            raise MotionError('Mask time/feather must be nonnegative and iterations between 1 and 50.')
    else:
        if (not np.isfinite([a.start, a.duration, a.fps, a.fb_threshold, a.ransac_threshold,
                              a.min_inlier_ratio, a.min_coverage]).all()
                or a.start < 0 or min(a.duration, a.fps, a.fb_threshold, a.ransac_threshold) <= 0
                or not 0 < a.min_inlier_ratio <= 1 or not 0 <= a.min_coverage <= 1
                or a.min_points < 3 or a.max_features < a.min_points):
            raise MotionError('Invalid time, feature-count, confidence, coverage, or flow threshold setting.')
        a.count = int(round(a.duration*a.fps)); a.duration = a.count/a.fps
        if a.count < 2: raise MotionError('Select at least two video samples.')
    if a.command == 'stabilize':
        if (a.width < 2 or a.height < 2 or a.width % 2 or a.height % 2
                or not np.isfinite([a.strength, a.smoothing, a.zoom, a.max_zoom]).all()
                or not 0 <= a.strength <= 1 or a.smoothing < 0 or not 1 <= a.zoom <= a.max_zoom
                or not 0 <= a.crf <= 51):
            raise MotionError('Stabilization needs even dimensions, strength in [0,1], nonnegative smoothing, and 1 <= zoom <= max-zoom.')


def atomic_json(path, data):
    text = json.dumps(data, ensure_ascii=False, indent=2)+'\n'
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=str(path.parent), prefix='.cv-report-', suffix='.json', delete=False) as f:
            tmp = Path(f.name); f.write(text.encode())
        os.replace(tmp, path)
    finally:
        if tmp and tmp.exists(): tmp.unlink()


def publish_bundle(artifacts):
    """Stage all files first, then roll back ordinary replace failures.

    This is not a cross-file atomic transaction and does not coordinate with
    concurrent external writers. Each single-file replacement is atomic.
    """
    with ExitStack() as stack:
        prepared, replaced = [], []
        for source, destination in artifacts:
            destination = Path(destination)
            folder = Path(stack.enter_context(tempfile.TemporaryDirectory(
                prefix='.cv-publish-', dir=str(destination.parent))))
            staged, backup = folder/('new'+destination.suffix), folder/'previous'
            shutil.copy2(source, staged)
            existed = destination.exists()
            if existed:
                if not destination.is_file() or destination.is_symlink():
                    raise MotionError('An output/report target changed into an unsupported file type.')
                shutil.copy2(destination, backup)
            prepared.append((staged, destination, backup if existed else None))
        try:
            for staged, destination, backup in prepared:
                os.replace(staged, destination)
                replaced.append((destination, backup))
        except BaseException:
            rollback_errors = []
            for destination, backup in reversed(replaced):
                try:
                    if backup is not None: os.replace(backup, destination)
                    else: destination.unlink()
                except OSError as exc:
                    rollback_errors.append(f'{destination.name}: {exc}')
            if rollback_errors:
                raise MotionError('Publication failed and rollback could not restore every file: '+'; '.join(rollback_errors))
            raise


def run(a):
    validate_args(a)
    reader = Reader(a.input, a.ffmpeg)
    try:
        if a.command == 'mask':
            source = reader.at(a.time)
            mask, report = static_grabcut(source, a.roi, a.foreground_point, a.background_point,
                a.iterations, a.analysis_width, a.feather)
            report.update(input_name=reader.path.name, time=a.time,
                          source_frame=None if reader.still else int(math.floor(a.time*reader.fps+1e-7)),
                          timing='Static frame extraction; CFR frame-index assumption; VFR not detected')
            with tempfile.TemporaryDirectory(prefix='.cv-mask-', dir=str(a.output.parent)) as temp:
                target = Path(temp)/'mask.png'
                if not cv2.imwrite(str(target), mask): raise MotionError('PNG encoding failed.')
                if cv2.imread(str(target), 0).shape != mask.shape: raise MotionError('Encoded mask dimensions changed.')
                details = Path(temp)/'report.json'
                details.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
                publish_bundle([(target, a.output), (details, a.report)])
            print(json.dumps(dict(output=str(a.output), report=str(a.report), foreground_fraction=report['foreground_fraction'])))
            return
        if not reader.still: reader.check_range(a.start, a.start+a.duration)
        if reader.still and a.command == 'stabilize': raise MotionError('Stabilization requires a video, not a still image.')
        count = 1 if reader.still else a.count
        result = analyze_frames((reader.at(a.start+i/a.fps) for i in range(count)), a)
        report = analysis_report(result, a, reader)
        if a.command == 'analyze':
            atomic_json(a.output, report)
            print(json.dumps(dict(output=str(a.output), quality=report['quality'])))
            return
        failures = report['quality']['rejected_intervals']
        if failures and a.fallback == 'stop':
            raise PreparationRejected('Dominant-motion model rejected; select a more static textured background ROI or use analyze for diagnostics.',
                                      dict(rejected_intervals=failures, valid_pair_fraction=report['quality']['valid_pair_fraction']))
        maps, stabilization = stabilization_maps(result, (a.width, a.height), a.fps,
            a.smoothing, a.strength, a.zoom, a.max_zoom, not a.no_auto_crop)
        report.update(stabilization=stabilization, audio='omitted; silent preparation output',
                      fallback=a.fallback, failed_pairs_assumed_zero_incremental_motion=bool(failures),
                      quality_review='Encoded geometry is bounded; judge parallax, rolling shutter and moving objects visually. No 3D or nonrigid correction is performed.')
        with tempfile.TemporaryDirectory(prefix='.cv-stabilize-', dir=str(a.output.parent)) as temp:
            video = Path(temp)/'stable.mp4'
            def frames():
                for i, matrix in enumerate(maps):
                    yield cv2.warpAffine(reader.at(a.start+i/a.fps), matrix, (a.width, a.height),
                        flags=cv2.INTER_CUBIC | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_CONSTANT)
            write_video(a.ffmpeg, video, frames(), (a.width, a.height), a.fps, a.count, a.crf)
            report['validation'] = validate_video(a.ffmpeg, video, (a.width, a.height), a.count, a.fps)
            details = Path(temp)/'report.json'
            details.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
            publish_bundle([(video, a.output), (details, a.report)])
        print(json.dumps(dict(output=str(a.output), report=str(a.report), zoom=stabilization['zoom'],
                              valid_pair_fraction=report['quality']['valid_pair_fraction'], **report['validation'])))
    finally:
        reader.close()


def main():
    parser = build_parser(); a = parser.parse_args()
    try:
        run(a)
    except PreparationRejected as exc:
        parser.exit(2, json.dumps(dict(status='rejected', reason=str(exc), **exc.details))+'\n')
    except (MotionError, cv2.error, OSError, np.linalg.LinAlgError) as exc:
        parser.exit(2, f'CV preparation: {exc}\n')


if __name__ == '__main__':
    main()
