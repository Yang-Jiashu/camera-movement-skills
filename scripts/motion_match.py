"""Estimate cross-shot visual anchors from verified local feature correspondences.

This is a planar/local image registration aid, not semantic matching or 3D pose.
Low-texture, repeated-structure and geometrically inconsistent pairs are rejected.
"""
from pathlib import Path
import argparse
import json
import shutil

import cv2
import numpy as np

from motion_core import MotionError, Reader
from prepare_motion import atomic_json


def match_images(first, second, analysis_width=960, min_inliers=12,
                 min_inlier_ratio=.45, ratio=.72, ransac_threshold=3.):
    if (analysis_width < 64 or min_inliers < 6 or not 0 < min_inlier_ratio <= 1
            or not 0 < ratio < 1 or ransac_threshold <= 0):
        raise MotionError('Invalid feature registration settings.')
    frames, scales = [], []
    for image in [first, second]:
        h, w = image.shape[:2]
        scale = min(1., analysis_width/w)
        size = (round(w*scale), round(h*scale))
        frames.append(cv2.resize(image, size, interpolation=cv2.INTER_AREA))
        scales.append(np.diag([size[0]/w, size[1]/h, 1.]))
    detector = cv2.SIFT_create(nfeatures=3500)
    features = [detector.detectAndCompute(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), None) for f in frames]
    (ka, da), (kb, db) = features
    if da is None or db is None or min(len(ka), len(kb)) < min_inliers:
        raise MotionError('Insufficient texture for automatic matching; supply explicit corresponding anchors.')
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    forward = matcher.knnMatch(da, db, k=2)
    backward = matcher.knnMatch(db, da, k=2)
    reverse = {pair[0].queryIdx: pair[0].trainIdx for pair in backward
               if len(pair) == 2 and pair[0].distance < ratio*pair[1].distance}
    good = [pair[0] for pair in forward if len(pair) == 2
            and pair[0].distance < ratio*pair[1].distance
            and reverse.get(pair[0].trainIdx) == pair[0].queryIdx]
    if len(good) < min_inliers:
        raise MotionError('Too few mutual feature matches; choose a shared rigid structure or explicit anchors.')
    a = np.float32([ka[m.queryIdx].pt for m in good])
    z = np.float32([kb[m.trainIdx].pt for m in good])
    homography, flags = cv2.findHomography(a, z, cv2.RANSAC, ransac_threshold,
                                          maxIters=4000, confidence=.995)
    if homography is None or not np.isfinite(homography).all():
        raise MotionError('Feature registration has no finite homography.')
    valid = flags.ravel().astype(bool)
    if int(valid.sum()) < min_inliers or float(valid.mean()) < min_inlier_ratio:
        raise MotionError('Feature geometry failed the inlier gate; automatic anchors were not accepted.')
    hulls = [cv2.convexHull(points[valid]) for points in [a, z]]
    coverage = [float(cv2.contourArea(hull)/(f.shape[0]*f.shape[1])) for hull, f in zip(hulls, frames)]
    if min(coverage) < .02:
        raise MotionError('Matches are too localized to validate a shared region; supply explicit anchors.')
    anchor_small = np.median(a[valid], axis=0)
    mapped = cv2.perspectiveTransform(anchor_small.reshape(1, 1, 2), homography)[0, 0]
    projected = cv2.perspectiveTransform(a[valid].reshape(1, -1, 2), homography)[0]
    error = np.linalg.norm(projected-z[valid], axis=1)
    # Reject a locally folded or implausibly scaled mapping at the anchor.
    local = np.float32([anchor_small, anchor_small+[10, 0], anchor_small+[0, 10]])
    mapped_local = cv2.perspectiveTransform(local.reshape(1, -1, 2), homography)[0]
    jacobian = np.column_stack([(mapped_local[1]-mapped_local[0])/10,
                                (mapped_local[2]-mapped_local[0])/10])
    singular = np.linalg.svd(jacobian, compute_uv=False)
    if np.linalg.det(jacobian) <= 0 or singular.min() < .1 or singular.max() > 10:
        raise MotionError('Registered anchor has folded/extreme geometry; inspect the pair manually.')
    an = anchor_small/np.array([frames[0].shape[1]-1, frames[0].shape[0]-1])
    bn = mapped/np.array([frames[1].shape[1]-1, frames[1].shape[0]-1])
    if np.any(an < .02) or np.any(an > .98) or np.any(bn < .02) or np.any(bn > .98):
        raise MotionError('Automatic anchor is too close to an edge; choose a crop-safe shared region.')
    original_h = np.linalg.inv(scales[1])@homography@scales[0]
    original_h /= original_h[2, 2]
    report = dict(method='SIFT + mutual ratio test + RANSAC homography',
        anchor_a=an.tolist(), anchor_b=bn.tolist(), homography_source_pixels=original_h.tolist(),
        matched_features=len(good), inliers=int(valid.sum()), inlier_ratio=float(valid.mean()),
        inlier_coverage=coverage, median_error_analysis_pixels=float(np.median(error)),
        analysis_sizes=[[f.shape[1], f.shape[0]] for f in frames],
        semantic_identity_verified=False,
        limitations='Correspondence supports a local/planar registration hypothesis. Review repeated structures, parallax and selected anchor identity.')
    inlier_matches = [m for m, accepted in zip(good, valid) if accepted]
    shown = [inlier_matches[i] for i in np.linspace(0, len(inlier_matches)-1,
               min(60, len(inlier_matches))).astype(int)]
    montage = cv2.drawMatches(frames[0], ka, frames[1], kb, shown, None,
        flags=cv2.DrawMatchesFlags_NOT_DRAW_SINGLE_POINTS, matchColor=(130, 115, 65))
    for point, label in [(anchor_small, 'A anchor'), (mapped+[frames[0].shape[1], 0], 'B anchor')]:
        point = tuple(np.rint(point).astype(int))
        cv2.circle(montage, point, 16, (0, 0, 0), 5)
        cv2.drawMarker(montage, point, (30, 245, 255), cv2.MARKER_CROSS, 30, 3)
        cv2.putText(montage, label, (point[0]+20, max(30, point[1]-15)),
                    cv2.FONT_HERSHEY_SIMPLEX, .8, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(montage, label, (point[0]+20, max(30, point[1]-15)),
                    cv2.FONT_HERSHEY_SIMPLEX, .8, (30, 245, 255), 2, cv2.LINE_AA)
    report['visualization'] = dict(shown_inlier_matches=len(shown), total_inliers=len(inlier_matches),
                                   anchors='yellow crosses; lines are a display subset only')
    return report, montage


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', type=Path, required=True)
    p.add_argument('--input-b', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True, help='JSON with anchors and registration diagnostics.')
    p.add_argument('--time', type=float, default=0.)
    p.add_argument('--time-b', type=float, default=0.)
    p.add_argument('--analysis-width', type=int, default=960)
    p.add_argument('--ffmpeg', default=shutil.which('ffmpeg'))
    p.add_argument('--overwrite', action='store_true')
    a = p.parse_args()
    readers = []
    try:
        if not a.ffmpeg: raise MotionError('Supply --ffmpeg or put FFmpeg on PATH.')
        if a.output.suffix.lower() != '.json': raise MotionError('Output must be JSON.')
        if a.output.resolve() in [a.input.resolve(), a.input_b.resolve()]: raise MotionError('Output must not replace an input.')
        if a.output.exists() and not a.overwrite: raise MotionError('Output exists; use --overwrite.')
        readers = [Reader(a.input, a.ffmpeg), Reader(a.input_b, a.ffmpeg)]
        report, _ = match_images(readers[0].at(a.time), readers[1].at(a.time_b), a.analysis_width)
        report['sample_times'] = [a.time, a.time_b]
        a.output.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(a.output, report)
        print(json.dumps(dict(output=str(a.output), anchor_a=report['anchor_a'], anchor_b=report['anchor_b'])))
    except (MotionError, cv2.error, OSError) as exc:
        p.exit(2, f'camera matching: {exc}\n')
    finally:
        for reader in readers: reader.close()


if __name__ == '__main__':
    main()
