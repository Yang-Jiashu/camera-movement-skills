#!/usr/bin/env python3
"""Prompted classical video masks and verified straight-edge occlusion reveals.

No semantic model or depth model runs. Dense DIS/Farneback flow is checked in
both directions, then a local GrabCut updates the prompted object's silhouette.
A reveal is a separately fitted trailing edge of an explicitly selected solid
pillar/doorframe, never the object mask itself. CFR frame indexing is assumed;
VFR is not detected. Tagged HDR is rejected by motion_core.Reader.
"""
from pathlib import Path
import argparse
import json
import math
import os
import shutil
import sys
import tempfile

import cv2
import numpy as np

from motion_core import MotionError, Reader, unit_point


class MaskRejected(MotionError):
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}


def roi_pixels(roi, size):
    w, h = size
    if roi is None or len(roi) != 4 or not np.isfinite(roi).all():
        raise MotionError('Provide --roi normalized X Y WIDTH HEIGHT, or --initial-mask.')
    x, y, rw, rh = roi
    if min(x, y) < 0 or min(rw, rh) <= 0 or x+rw > 1 or y+rh > 1:
        raise MotionError('ROI must fit inside normalized [0,1] coordinates.')
    a, b = np.floor(np.array([x, y])*[w, h]).astype(int)
    c, d = np.ceil(np.array([x+rw, y+rh])*[w, h]).astype(int)
    if min(c-a, d-b) < 4 or (c-a)*(d-b) >= w*h:
        raise MotionError('ROI needs at least 4 pixels per side and some known background outside it.')
    return a, b, c, d


def solid_components(mask, reference=None, fill_holes=False):
    """Retain components supported by the propagated prompt; optionally solidify a pillar."""
    mask = (mask > 0).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if count <= 1:
        return mask
    selected = np.zeros_like(mask)
    largest = max(stats[1:, cv2.CC_STAT_AREA])
    for i in range(1, count):
        part = labels == i
        support = int(np.count_nonzero(part & (reference > 0))) if reference is not None else stats[i, cv2.CC_STAT_AREA]
        if stats[i, cv2.CC_STAT_AREA] >= max(4, largest*.002) and support >= max(2, stats[i, cv2.CC_STAT_AREA]*.03):
            selected[part] = 1
    if fill_holes:
        contours, _ = cv2.findContours(selected, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(selected, contours, -1, 1, cv2.FILLED)
    return selected


def initialize_mask(image, roi=None, foreground_points=(), background_points=(), initial_mask=None,
                    iterations=5, solid=False):
    h, w = image.shape[:2]
    if initial_mask is not None:
        if initial_mask.shape != (h, w):
            raise MotionError('Initial mask dimensions must match the analysis image.')
        result = (initial_mask >= 128).astype(np.uint8)
        method = 'user_initial_mask'
    else:
        x0, y0, x1, y1 = roi_pixels(roi, (w, h))
        labels = np.full((h, w), cv2.GC_BGD, np.uint8)
        labels[y0:y1, x0:x1] = cv2.GC_PR_FGD
        seeds = []
        for points, kind in [(foreground_points, 'foreground'), (background_points, 'background')]:
            seed = np.zeros((h, w), np.uint8)
            for point in points:
                nx, ny = unit_point(point, kind+' point')
                px, py = round(nx*(w-1)), round(ny*(h-1))
                if kind == 'foreground' and not (x0 <= px < x1 and y0 <= py < y1):
                    raise MotionError('Foreground points must lie inside the ROI.')
                cv2.circle(seed, (px, py), max(1, round(min(w, h)*.004)), 1, -1)
            seeds.append(seed)
        if np.any(seeds[0] & seeds[1]):
            raise MotionError('Foreground and background point regions overlap.')
        labels[seeds[0] > 0] = cv2.GC_FGD
        labels[seeds[1] > 0] = cv2.GC_BGD
        bg, fg = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
        cv2.setRNGSeed(0)
        cv2.grabCut(image, labels, None, bg, fg, iterations, cv2.GC_INIT_WITH_MASK)
        result = np.isin(labels, [cv2.GC_FGD, cv2.GC_PR_FGD]).astype(np.uint8)
        if seeds[0].any() and np.mean(result[seeds[0] > 0]) < .95:
            raise MaskRejected('Initialization did not retain the foreground prompts.')
        method = 'user_roi_points_grabcut'
    result = solid_components(result, fill_holes=solid)
    fraction = float(result.mean())
    if not .0005 < fraction < .98:
        raise MaskRejected('Initialization is empty, too small, or covers nearly the entire frame.', {'foreground_fraction': fraction})
    return result, {'method': method, 'foreground_fraction': fraction}


def dense_flow(a, b, method='dis'):
    if method == 'dis':
        if not hasattr(cv2, 'DISOpticalFlow_create'):
            raise MotionError('This OpenCV build lacks DIS; choose --flow farneback.')
        engine = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
        engine.setUseSpatialPropagation(True)
        return engine.calc(a, b, None)
    return cv2.calcOpticalFlowFarneback(a, b, None, .5, 5, 25, 4, 7, 1.5, 0)


def flow_warp(previous, current, mask, method='dis', fb_threshold=2.):
    """Backward warp and independent forward/backward consistency, in analysis pixels."""
    a = cv2.cvtColor(previous, cv2.COLOR_BGR2GRAY)
    b = cv2.cvtColor(current, cv2.COLOR_BGR2GRAY)
    forward, backward = dense_flow(a, b, method), dense_flow(b, a, method)
    h, w = a.shape
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    mapx, mapy = xx+backward[..., 0], yy+backward[..., 1]
    valid = (mapx >= 0) & (mapx <= w-1) & (mapy >= 0) & (mapy <= h-1)
    inverse = cv2.remap(forward, mapx, mapy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    residual = np.linalg.norm(backward+inverse, axis=2)
    warped = cv2.remap(mask.astype(np.float32), mapx, mapy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    oldgray = cv2.remap(a, mapx, mapy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    photo = np.abs(oldgray.astype(np.float32)-b)
    reliable = valid & (residual <= fb_threshold) & (photo <= 40)
    # Motion is measured in the source object's interior, with its own forward/back test.
    fx, fy = xx+forward[..., 0], yy+forward[..., 1]
    reverse = cv2.remap(backward, fx, fy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    forward_good = ((mask > 0) & (fx >= 0) & (fx <= w-1) & (fy >= 0) & (fy <= h-1)
                    & (np.linalg.norm(forward+reverse, axis=2) <= fb_threshold))
    motion = np.median(forward[forward_good], axis=0) if forward_good.any() else np.zeros(2)
    fg = warped >= .5
    fraction = float(reliable[fg].mean()) if fg.any() else 0.
    metrics = {'flow': method, 'foreground_fb_consistent_fraction': fraction,
               'median_fb_error_analysis_pixels': float(np.median(residual[fg])) if fg.any() else None,
               'median_photometric_error': float(np.median(photo[fg])) if fg.any() else None,
               'measured_displacement_analysis_pixels': motion.tolist()}
    return warped, reliable, motion, metrics


def bbox(mask):
    y, x = np.nonzero(mask)
    return None if not len(x) else [int(x.min()), int(y.min()), int(x.max()+1), int(y.max()+1)]


def exit_evidence(mask, motion, size, minimum_motion=.15):
    """A disappearance is legal only through a frame border already touched by the object."""
    box = bbox(mask)
    if box is None:
        return None
    w, h = size
    x0, y0, x1, y1 = box
    dx, dy = motion
    possibilities = []
    if x0 <= 1 and dx < -minimum_motion and x1+dx <= max(3., -dx*1.5): possibilities.append('left')
    if x1 >= w-1 and dx > minimum_motion and x0+dx >= w-max(3., dx*1.5): possibilities.append('right')
    if y0 <= 1 and dy < -minimum_motion and y1+dy <= max(3., -dy*1.5): possibilities.append('up')
    if y1 >= h-1 and dy > minimum_motion and y0+dy >= h-max(3., dy*1.5): possibilities.append('down')
    return possibilities[0] if possibilities else None


def appearance_support(previous, current, mask):
    """Small prompted foreground color palette, used only to reject unsupported seeds."""
    old = cv2.cvtColor(previous, cv2.COLOR_BGR2LAB).astype(np.float32)
    new = cv2.cvtColor(current, cv2.COLOR_BGR2LAB).astype(np.float32)
    samples = old[mask > 0]
    if len(samples) > 4096:
        samples = samples[np.linspace(0, len(samples)-1, 4096).astype(int)]
    clusters = min(5, max(1, len(samples)//12))
    cv2.setRNGSeed(0)
    _, labels, centers = cv2.kmeans(samples, clusters, None,
        (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, .2), 1, cv2.KMEANS_PP_CENTERS)
    old_distance = np.linalg.norm(samples-centers[labels[:, 0]], axis=1)
    threshold = max(16., min(45., float(np.percentile(old_distance, 95))*1.8))
    distance = np.full(mask.shape, np.inf, np.float32)
    for center in centers:
        distance = np.minimum(distance, np.linalg.norm(new-center, axis=2))
    return distance <= threshold, threshold


def propagate_pair(previous, current, mask, settings, previous_motion=None):
    warped, reliable, motion, report = flow_warp(previous, current, mask, settings.flow, settings.fb_threshold)
    h, w = mask.shape
    # Boundary loss makes motion unreliable at the last sliver. Use the preceding
    # measured flow only for border-exit evidence, never as a substitute for a mask.
    motion_for_exit = motion
    if previous_motion is not None and np.linalg.norm(motion) < .4*np.linalg.norm(previous_motion):
        motion_for_exit = previous_motion
    exit_side = exit_evidence(mask, motion_for_exit, (w, h))
    candidate = warped >= .45
    prior_area = int(mask.sum())
    if candidate.sum() < 4 and exit_side:
        report.update(accepted=True, state='observed_border_exit', exit_side=exit_side,
                      foreground_fraction=0., bbox_analysis_pixels=None)
        return np.zeros_like(mask), motion_for_exit, report
    margin = settings.search_margin
    radius = max(3, int(margin))
    search = cv2.dilate(candidate.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*radius+1, 2*radius+1))) > 0
    # Include a measured-motion prediction so background-anchored dense flow at a
    # low-texture boundary cannot truncate the local GrabCut search window.
    predicted = cv2.warpAffine(mask, np.array([[1, 0, motion_for_exit[0]], [0, 1, motion_for_exit[1]]], np.float32), (w, h)) > 0
    search |= cv2.dilate(predicted.astype(np.uint8), np.ones((2*radius+1, 2*radius+1), np.uint8)) > 0
    if not candidate.any() or not search.any():
        raise MaskRejected('Object vanished without verified border-exit evidence.', report)
    erosion = max(1, min(5, radius//4))
    core = cv2.erode(candidate.astype(np.uint8), np.ones((2*erosion+1, 2*erosion+1), np.uint8)) > 0
    color_ok, color_threshold = appearance_support(previous, current, mask)
    core &= reliable & color_ok
    report.update(appearance_threshold_lab=color_threshold, reliable_foreground_seed_pixels=int(core.sum()))
    if not core.any():
        if exit_side:
            report.update(accepted=True, state='observed_border_exit', exit_side=exit_side,
                          foreground_fraction=0., bbox_analysis_pixels=None)
            return np.zeros_like(mask), motion_for_exit, report
        raise MaskRejected('No reliable foreground core remains; supply a better prompt or shorter interval.', report)
    labels = np.full((h, w), cv2.GC_BGD, np.uint8)
    labels[search] = cv2.GC_PR_BGD
    labels[candidate] = cv2.GC_PR_FGD
    labels[core] = cv2.GC_FGD
    if np.all(search):
        # A foreground/background color model is not identifiable without known background.
        raise MaskRejected('Local segmentation has no known background; use a shorter interval or smaller object.', report)
    bg, fg = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    cv2.setRNGSeed(0)
    cv2.grabCut(current, labels, None, bg, fg, settings.refine_iterations, cv2.GC_INIT_WITH_MASK)
    result = np.isin(labels, [cv2.GC_FGD, cv2.GC_PR_FGD]).astype(np.uint8)
    result = solid_components(result, candidate, fill_holes=settings.mode == 'reveal')
    union = np.count_nonzero((result > 0) | candidate)
    iou = np.count_nonzero((result > 0) & candidate)/max(1, union)
    ratio = float(result.sum()/max(1, prior_area))
    appearance_fraction = float(color_ok[result > 0].mean()) if result.any() else 0.
    report.update(appearance_supported_fraction=appearance_fraction, refinement_iou=float(iou), area_ratio=ratio, foreground_fraction=float(result.mean()),
                  bbox_analysis_pixels=bbox(result), accepted=False, state='visible')
    if result.sum() < 4:
        if exit_side:
            report.update(accepted=True, state='observed_border_exit', exit_side=exit_side)
            return result*0, motion_for_exit, report
        raise MaskRejected('Segmentation became empty away from a verified exit.', report)
    minimum_fraction = .005 if settings.mode == 'reveal' else .15
    if report['foreground_fb_consistent_fraction'] < minimum_fraction or core.sum() < 8 or iou < .2 or appearance_fraction < .6:
        raise MaskRejected('Dense-flow propagation, foreground appearance, and local segmentation disagree.', report)
    if report['foreground_fb_consistent_fraction'] < .15:
        report['warning'] = 'limited_dense_support; straight-edge geometry must independently pass'
    if not .15 <= ratio <= 5 and not exit_side:
        raise MaskRejected('Abrupt foreground area change; tracking may have switched objects.', report)
    if result.mean() > .98:
        raise MaskRejected('Foreground consumes almost the full frame; segmentation cannot be verified.', report)
    report['accepted'] = True
    old_box, new_box = bbox(mask), bbox(result)
    silhouette_motion = np.array(motion_for_exit, dtype=float)
    for axis, limit in [(0, w), (1, h)]:
        observed = [new_box[k]-old_box[k] for k in (axis, axis+2)
                    if 1 < old_box[k] < limit-1 and 1 < new_box[k] < limit-1]
        if observed:
            silhouette_motion[axis] = float(np.median(observed))
    report['measured_silhouette_displacement_analysis_pixels'] = silhouette_motion.tolist()
    return result, silhouette_motion, report


def track_masks(frame_at, count, initial_index, initial_mask, settings, save_mask):
    """Bidirectional propagation, retaining only two frames and masks in memory."""
    reports = [None]*count
    initial_image = frame_at(initial_index)
    save_mask(initial_index, initial_mask)
    reports[initial_index] = {'frame': initial_index, 'accepted': True, 'state': 'user_initialized',
                              'foreground_fraction': float(initial_mask.mean()), 'bbox_analysis_pixels': bbox(initial_mask)}
    for step in (-1, 1):
        previous, mask, motion = initial_image, initial_mask, None
        exited = None
        for index in range(initial_index+step, count if step > 0 else -1, step):
            current = frame_at(index)
            if exited is not None:
                # Explicit single-pass assumption: once a verified border exit
                # occurs this branch remains outside. No missed in-frame mask is filled.
                supported, _ = appearance_support(initial_image, current, initial_mask)
                strip = max(3, min(settings.search_margin, round(min(supported.shape)*.04)))
                region = (supported[:, :strip] if exited == 'left' else supported[:, -strip:] if exited == 'right'
                          else supported[:strip, :].T if exited == 'up' else supported[-strip:, :].T)
                spanning = float(np.mean(region.mean(axis=1) >= .6))
                if spanning >= .75:
                    raise MaskRejected('Foreground-like spanning content reappeared at the exit border; a single pass is required.',
                                       {'frame': index, 'exit_side': exited, 'outside_border_appearance_support': spanning})
                report = {'accepted': True, 'state': 'outside_after_verified_exit', 'exit_side': exited,
                          'outside_border_appearance_support': spanning,
                          'foreground_fraction': 0., 'bbox_analysis_pixels': None}
                mask = np.zeros_like(initial_mask)
            else:
                try:
                    mask, motion, report = propagate_pair(previous, current, mask, settings, motion)
                except MaskRejected as error:
                    error.details.update(frame=index, propagation_direction=step)
                    raise
                if report['state'] == 'observed_border_exit':
                    exited = report['exit_side']
            report.update(frame=index, propagation_direction=step)
            reports[index] = report
            save_mask(index, mask)
            previous = current
    return reports


def fit_reveal_edge(mask, direction, min_support=.75, max_error=3., image=None):
    """Fit a straight trailing boundary; return B alpha independently of object alpha."""
    h, w = mask.shape
    vertical = direction in ('left', 'right')
    data = mask if vertical else mask.T
    valid = np.any(data > 0, axis=1)
    support = float(valid.mean())
    if support < min_support:
        # A genuinely measured final sliver can occupy only a few scanlines.
        # Permit only a <=1.5% border fragment, with the boundary at the border;
        # every interior frame still has to satisfy the spanning model.
        box = bbox(mask)
        narrow_border = (box is not None and mask.mean() < .015 and
            ((direction == 'right' and box[0] == 0 and box[2] <= max(3, math.ceil(w*.02))) or
             (direction == 'left' and box[2] == w and box[0] >= w-max(3, math.ceil(w*.02))) or
             (direction == 'up' and box[3] == h and box[1] >= h-max(3, math.ceil(h*.02))) or
             (direction == 'down' and box[1] == 0 and box[3] <= max(3, math.ceil(h*.02)))))
        if not narrow_border:
            raise MaskRejected('Selected object does not form a sufficiently spanning occlusion boundary.', {'edge_support_fraction': support})
        # Use actual silhouette coverage at the last border sliver, not a fitted
        # extrapolation into rows where no object is observed.
        swept = np.ones(data.shape, np.float32)
        for row, values in enumerate(data):
            positions = np.flatnonzero(values)
            if len(positions):
                if direction in ('right', 'down'): swept[row, :positions[-1]+1] = 0
                else: swept[row, positions[0]:] = 0
        alpha = swept if vertical else swept.T
        return alpha, {'edge_support_fraction': support, 'border_sliver': True, 'b_fraction': float(alpha.mean())}
    rows = np.flatnonzero(valid)
    take_max = direction in ('right', 'down')
    positions = []
    for row in data[valid]:
        indices = np.flatnonzero(row)
        positions.append(indices[-1]+1 if take_max else indices[0])
    positions = np.array(positions, np.float64)
    # RANSAC rejects local GrabCut foliage leaks rather than interpreting them
    # as the rigid pillar boundary. Its coverage and all-point error stay visible.
    rng = np.random.default_rng(0)
    best = None
    if len(rows) < 2:
        raise MaskRejected('Too few scanlines for an occlusion edge.')
    candidates = [np.polyfit(rows, positions, 1)]
    for _ in range(350):
        a, b = rng.choice(len(rows), 2, replace=False)
        if abs(rows[a]-rows[b]) >= max(3, len(data)*.2):
            slope = (positions[b]-positions[a])/(rows[b]-rows[a])
            candidates.append(np.array([slope, positions[a]-slope*rows[a]]))
    for candidate in candidates:
        if abs(candidate[0]) > .35: continue
        error = np.abs(positions-np.polyval(candidate, rows))
        keep = error <= max_error
        score = (int(keep.sum()), -float(np.median(error[keep])) if keep.any() else -math.inf)
        if best is None or score > best[0]: best = score, candidate, keep
    if best is None or best[2].mean() < .55:
        raise MaskRejected('No dominant straight occluder boundary; curved or fragmented masks are unsupported.',
                           {'edge_support_fraction': support, 'edge_ransac_inlier_fraction': 0. if best is None else float(best[2].mean())})
    line = np.polyfit(rows[best[2]], positions[best[2]], 1)
    error = np.abs(positions-np.polyval(line, rows))
    keep = error <= max_error
    p95 = float(np.percentile(error, 95))
    inlier_error = float(np.percentile(error[keep], 95))
    evidence = None
    midpoint = float(np.polyval(line, (len(data)-1)/2))
    limit = data.shape[1]
    clipped = midpoint <= 1 or midpoint >= limit-1
    if image is not None and not clipped:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 35, 90)
        if not vertical: edges = edges.T
        radius = max(2, math.ceil(max_error))
        near = cv2.dilate(edges, np.ones((1, 2*radius+1), np.uint8)) > 0
        coordinate = np.rint(np.polyval(line, np.arange(len(data)))).astype(int)
        inside = (coordinate >= 0) & (coordinate < limit)
        evidence = float(near[np.arange(len(data))[inside], coordinate[inside]].mean()) if inside.any() else 0.
        if evidence < .5:
            raise MaskRejected('Fitted mask boundary lacks independent image-edge support.',
                               {'image_edge_support_fraction': evidence, 'edge_ransac_inlier_fraction': float(keep.mean())})
    # At least 90% of each visible scanline must be one solid band: a person,
    # branches, or disconnected features are not a valid pillar-like wipe.
    solidity = []
    for row in data[valid]:
        indices = np.flatnonzero(row)
        solidity.append(len(indices)/(indices[-1]-indices[0]+1))
    if np.percentile(solidity, 10) < .90:
        raise MaskRejected('Occluder is not a solid spanning band.', {'scanline_solidity_p10': float(np.percentile(solidity, 10))})
    edge = np.polyval(line, np.arange(len(data)))
    coordinate = np.arange(data.shape[1], dtype=float)[None, :]+.5
    # Pixel coverage yields one-pixel anti-aliasing while preserving measured geometry.
    alpha = np.clip(coordinate-edge[:, None]+.5, 0, 1) if take_max else np.clip(edge[:, None]-coordinate+.5, 0, 1)
    if not vertical: alpha = alpha.T
    return alpha, {'edge_support_fraction': support, 'edge_p95_error_analysis_pixels': p95,
                   'edge_inlier_p95_error_analysis_pixels': inlier_error, 'edge_ransac_inlier_fraction': float(keep.mean()),
                   'image_edge_support_fraction': evidence, 'clipped_edge': clipped,
                   'edge_slope': float(line[0]), 'edge_midpoint_analysis_pixels': midpoint,
                   'scanline_solidity_p10': float(np.percentile(solidity, 10)), 'b_fraction': float(alpha.mean())}


def reveal_sequence(mask_at, reports, direction, save_reveal, min_support=.75, max_error=3., require_complete=False, frame_at=None):
    records = []
    entering = direction
    leaving = {'right': 'left', 'left': 'right', 'up': 'down', 'down': 'up'}[direction]
    for index, report in enumerate(reports):
        mask = mask_at(index)
        if mask.any():
            try:
                alpha, record = fit_reveal_edge(mask, direction, min_support, max_error, None if frame_at is None else frame_at(index))
            except MaskRejected as error:
                error.details['frame'] = index
                raise
        else:
            side = report.get('exit_side')
            # An empty mask is not evidence by itself. Only an observed exit
            # through one of the two relevant borders establishes reveal phase.
            if side not in (entering, leaving):
                raise MaskRejected('Empty occluder mask has no relevant border-exit evidence.', {'frame': index, 'exit_side': side})
            fraction = 0. if side == entering else 1.
            alpha = np.full(mask.shape, fraction, np.float32)
            record = {'b_fraction': fraction, 'edge_support_fraction': 0., 'outside_evidence': report['state'], 'exit_side': side}
        record['frame'] = index
        records.append(record)
        save_reveal(index, alpha)
    fractions = np.array([r['b_fraction'] for r in records])
    # Small pixel jitter is tolerated, but never silently made monotonic.
    tolerance = max(.006, 2/min(mask_at(0).shape))
    worst = float(np.min(np.diff(fractions))) if len(fractions) > 1 else 0.
    if worst < -tolerance:
        raise MaskRejected('Reveal edge reverses direction; select a monotonic single-pass interval.', {'worst_b_fraction_step': worst, 'records': records})
    complete = bool(fractions[0] <= .015 and fractions[-1] >= .985)
    if require_complete and not complete:
        raise MaskRejected('Measured occluder does not complete this wipe: choose an interval with full head/tail occlusion.',
                           {'first_b_fraction': float(fractions[0]), 'last_b_fraction': float(fractions[-1]), 'records': records})
    return {'direction': direction, 'b_enters_from': entering, 'edge_travels_toward': leaving,
            'complete': complete, 'first_b_fraction': float(fractions[0]), 'last_b_fraction': float(fractions[-1]),
            'worst_b_fraction_step': worst, 'records': records,
            'model': 'prompted solid straight-edge occluder; B occupies the swept trailing side, object alpha remains separate',
            'outside_assumption': 'single monotonic pass; border color support is checked after exit, but color checks cannot prove semantic absence or detect every re-entry'}


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument('--input', required=True, type=Path)
    p.add_argument('--output-dir', required=True, type=Path, help='New asset directory containing object/, optional reveal/, manifest.json.')
    p.add_argument('--start', type=float, default=0., help='Source start in seconds, using CFR frame-index timing.')
    p.add_argument('--duration', type=float, required=True, help='Selected source duration in seconds; no temporal resampling/interpolation beyond frame selection.')
    p.add_argument('--fps', type=float, default=30., help='Output mask cadence in frames/second; source frames are selected by CFR index.')
    p.add_argument('--init-frame', type=int, help='0-based output frame with visible foreground/background, default middle of selected interval.')
    p.add_argument('--roi', type=float, nargs=4, metavar=('X', 'Y', 'WIDTH', 'HEIGHT'), help='Initialization ROI, normalized [0,1] source coordinates.')
    p.add_argument('--initial-mask', type=Path, help='User alpha PNG at source dimensions, corresponding exactly to init-frame; alternative to ROI.')
    p.add_argument('--foreground-point', action='append', type=float, nargs=2, default=[], metavar=('X', 'Y'), help='Repeatable normalized [0,1] foreground point inside ROI.')
    p.add_argument('--background-point', action='append', type=float, nargs=2, default=[], metavar=('X', 'Y'), help='Repeatable normalized [0,1] known-background point.')
    p.add_argument('--mode', choices=['object', 'reveal'], default='object')
    p.add_argument('--reveal-direction', choices=['left', 'right', 'up', 'down'], default='right', help='Side from which B enters. Right: B is right of occluder trailing edge, which travels left.')
    p.add_argument('--require-complete-reveal', action='store_true', help='Reject unless measured reveal starts <=1.5%% B and finishes >=98.5%% B; never pads endpoints.')
    p.add_argument('--analysis-width', type=int, default=640, help='Maximum processing width, pixels; PNG assets return to source resolution.')
    p.add_argument('--flow', choices=['dis', 'farneback'], default='dis')
    p.add_argument('--fb-threshold', type=float, default=2., help='Dense forward/back consistency threshold, analysis pixels.')
    p.add_argument('--refine-iterations', type=int, default=2, help='Local GrabCut iterations per propagated frame.')
    p.add_argument('--search-margin', type=int, default=24, help='Local segmentation search radius around propagated mask, analysis pixels.')
    p.add_argument('--min-edge-support', type=float, default=.75, help='Minimum fraction of rows (vertical edge) or columns (horizontal edge) crossed by occluder.')
    p.add_argument('--max-edge-error', type=float, default=3., help='RANSAC boundary inlier distance, analysis pixels; require >=55%% inlier rows and independent image-edge support.')
    p.add_argument('--ffmpeg', default='ffmpeg')
    p.add_argument('--overwrite', action='store_true', help='Replace only a prior motion_masks asset bundle after all new assets validate.')
    return p


def validate_output(destination, sources, overwrite):
    destination = destination.absolute()
    if destination.is_symlink():
        raise MotionError('Output directory must not be a symbolic link.')
    for source in sources:
        source = source.resolve()
        if destination.resolve() == source or destination.resolve() in source.parents:
            raise MotionError('Output directory cannot contain or alias an input asset.')
    if destination.exists():
        if not destination.is_dir() or not overwrite:
            raise MotionError('Output already exists; use --overwrite only for a prior generated mask bundle.')
        try:
            old = json.loads((destination/'manifest.json').read_text())
        except (OSError, ValueError):
            raise MotionError('Refusing to overwrite a directory without a motion_masks manifest.')
        if old.get('generator') != 'motion_masks':
            raise MotionError('Refusing to overwrite a directory owned by another tool.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def publish_directory(payload, destination, backup):
    """Publish a complete directory, restoring the previous bundle on ordinary errors.

    A rename is atomic on its filesystem; the backup/replace sequence is not a
    multi-operation transaction and does not coordinate concurrent writers.
    """
    moved_old = False
    try:
        if destination.exists():
            os.replace(destination, backup)
            moved_old = True
        os.replace(payload, destination)
    except BaseException:
        if moved_old and not destination.exists():
            try:
                os.replace(backup, destination)
            except OSError as restore_error:
                # Keep the old bundle outside TemporaryDirectory cleanup.
                recovery = Path(tempfile.mkdtemp(prefix='.motion-masks-recovery-', dir=destination.parent))/'previous'
                shutil.move(str(backup), str(recovery))
                raise MotionError(f'Publication and rollback failed; previous assets preserved at {recovery}: {restore_error}')
        raise


def run(args):
    numeric = [args.start, args.duration, args.fps, args.fb_threshold, args.min_edge_support, args.max_edge_error]
    if not np.isfinite(numeric).all() or args.start < 0 or args.duration <= 0 or not 1 <= args.fps <= 120:
        raise MotionError('Require finite start>=0, duration>0 and fps in [1,120].')
    if not 64 <= args.analysis_width <= 1920 or not 1 <= args.refine_iterations <= 10 or not 2 <= args.search_margin <= 200:
        raise MotionError('Require analysis-width [64,1920], refine-iterations [1,10], search-margin [2,200].')
    if args.fb_threshold <= 0 or not .5 <= args.min_edge_support <= 1 or args.max_edge_error <= 0:
        raise MotionError('Require fb-threshold>0, min-edge-support [.5,1], max-edge-error>0.')
    if (args.roi is None) == (args.initial_mask is None):
        raise MotionError('Provide exactly one initialization: --roi or --initial-mask.')
    if args.initial_mask and (args.foreground_point or args.background_point):
        raise MotionError('Point prompts accompany --roi, not --initial-mask.')
    if args.require_complete_reveal and args.mode != 'reveal':
        raise MotionError('--require-complete-reveal requires --mode reveal.')
    count = int(math.ceil(args.duration*args.fps-1e-7))
    if count < 2 or count > 3600:
        raise MotionError('Selected interval must contain 2–3600 output frames; use short single-pass intervals.')
    initial_index = count//2 if args.init_frame is None else args.init_frame
    if not 0 <= initial_index < count:
        raise MotionError('--init-frame is outside the selected output frame interval.')
    sources = [args.input]+([args.initial_mask] if args.initial_mask else [])
    destination = validate_output(args.output_dir, sources, args.overwrite)
    reader = Reader(args.input, args.ffmpeg)
    try:
        if reader.still:
            raise MotionError('Temporal mask preparation requires a video input.')
        reader.check_range(args.start, args.start+(count-1)/args.fps)
        sw, sh = reader.width, reader.height
        scale = min(1., args.analysis_width/sw)
        aw, ah = round(sw*scale), round(sh*scale)
        with tempfile.TemporaryDirectory(prefix='.motion-masks-', dir=destination.parent) as td:
            temporary = Path(td)
            frames, payload = temporary/'frames', temporary/'payload'
            frames.mkdir(); payload.mkdir(); (payload/'object').mkdir()
            if args.mode == 'reveal': (payload/'reveal').mkdir()
            for i in range(count):
                image = reader.at(args.start+i/args.fps)
                if not cv2.imwrite(str(frames/f'{i:04d}.png'), cv2.resize(image, (aw, ah), interpolation=cv2.INTER_AREA)):
                    raise MotionError('Could not stage analysis frames.')
            reader.close()
            def frame_at(index):
                return cv2.imread(str(frames/f'{index:04d}.png'))
            initial = None
            if args.initial_mask:
                initial = cv2.imread(str(args.initial_mask), cv2.IMREAD_GRAYSCALE)
                if initial is None or initial.shape != (sh, sw):
                    raise MotionError('Initial alpha must be a readable image at exact source dimensions.')
                initial = cv2.resize(initial, (aw, ah), interpolation=cv2.INTER_NEAREST)
            initial, initialization = initialize_mask(frame_at(initial_index), args.roi, args.foreground_point,
                args.background_point, initial, max(5, args.refine_iterations), args.mode == 'reveal')
            # Keep analysis masks on disk too: bidirectional access does not load a whole video into RAM.
            masks = temporary/'analysis-masks'; masks.mkdir()
            def save_mask(index, mask):
                if not cv2.imwrite(str(masks/f'{index:04d}.png'), mask*255): raise MotionError('Could not stage mask.')
                full = cv2.resize(mask*255, (sw, sh), interpolation=cv2.INTER_NEAREST)
                if not cv2.imwrite(str(payload/'object'/f'{index:04d}.png'), full): raise MotionError('Could not stage source-size object alpha.')
            def mask_at(index):
                return (cv2.imread(str(masks/f'{index:04d}.png'), cv2.IMREAD_GRAYSCALE) > 0).astype(np.uint8)
            reports = track_masks(frame_at, count, initial_index, initial, args, save_mask)
            reveal = None
            if args.mode == 'reveal':
                def save_reveal(index, alpha):
                    full = cv2.resize(alpha.astype(np.float32), (sw, sh), interpolation=cv2.INTER_LINEAR)
                    if not cv2.imwrite(str(payload/'reveal'/f'{index:04d}.png'), np.rint(np.clip(full, 0, 1)*255).astype(np.uint8)):
                        raise MotionError('Could not stage reveal alpha.')
                reveal = reveal_sequence(mask_at, reports, args.reveal_direction, save_reveal, args.min_edge_support,
                                         args.max_edge_error, args.require_complete_reveal, frame_at)
            manifest = {'schema_version': 1, 'generator': 'motion_masks', 'status': 'accepted',
                'source': str(args.input.resolve()), 'source_size': [sw, sh], 'analysis_size': [aw, ah],
                'start_seconds': args.start, 'duration_seconds': count/args.fps, 'fps': args.fps, 'frame_count': count,
                'init_frame': initial_index, 'initialization': initialization, 'mode': args.mode,
                'object_pattern': 'object/%04d.png', 'reveal_pattern': 'reveal/%04d.png' if reveal else None,
                'mask_fps': args.fps, 'mask_start_number': 0, 'flow': args.flow,
                'timing': 'CFR frame-index selection; VFR not detected; no interpolation; assets contain no audio',
                'limits': ['user-supervised color segmentation, not semantic recognition',
                           'no learned segmentation or depth model', 'single object and single monotonic pass; outside-border appearance checks cannot prove semantic absence',
                           'flow consistency and straight-edge geometry are quality gates, not proof of correct object identity',
                           'inspect masks before final publication; complex articulated/transparent/motion-blurred occluders may be rejected'],
                'quality': {'limited_dense_support_frames': [r['frame'] for r in reports if 'warning' in r],
                            'note': 'Low dense support is accepted only in reveal mode with separate straight-edge and image-edge validation; this is not semantic confidence.'},
                'frames': reports, 'reveal': reveal}
            (payload/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
            for folder in ['object']+(['reveal'] if reveal else []):
                for i in range(count):
                    test = cv2.imread(str(payload/folder/f'{i:04d}.png'), cv2.IMREAD_GRAYSCALE)
                    if test is None or test.shape != (sh, sw): raise MotionError('Asset validation failed before publication.')
            backup = temporary/'previous'
            publish_directory(payload, destination, backup)
            return manifest
    finally:
        reader.close()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        manifest = run(args)
        print(json.dumps({'status': 'accepted', 'output_dir': str(args.output_dir.absolute()),
                          'frame_count': manifest['frame_count'], 'reveal': {k: v for k, v in (manifest['reveal'] or {}).items() if k != 'records'}}))
        return 0
    except (MotionError, cv2.error, OSError, ValueError) as error:
        details = error.details if isinstance(error, MaskRejected) else {}
        print(json.dumps({'status': 'rejected', 'error': str(error), 'details': details}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
