#!/usr/bin/env python3
"""Prepare honest, motion-bounded 2.5D layers from one SDR photograph.

GrabCut (or a supplied alpha) provides a foreground, classical inpainting removes
its occlusion from a background plate, and dense motion-footprint analysis limits
which repaired pixels may be revealed. Hidden inpainted pixels are not certified
as reconstructed scene content. No segmentation/depth model is downloaded.
"""
from dataclasses import dataclass, asdict, replace
from pathlib import Path
import argparse
import hashlib
import json
import math
import shutil
import tempfile

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from motion_core import MotionError, media_metadata, unit_point
from prepare_motion import static_grabcut, publish_bundle


@dataclass(frozen=True)
class LayerMotion:
    width: int = 1280
    height: int = 720
    fps: float = 30.
    duration: float = 4.
    foreground_scale_start: float = 1.2
    foreground_scale_end: float = 1.2
    background_scale_start: float = 1.2
    background_scale_end: float = 1.05
    foreground_shift_start: tuple = (0., 0.)
    foreground_shift_end: tuple = (0., 0.)
    background_shift_start: tuple = (0., 0.)
    background_shift_end: tuple = (0., 0.)
    anchor: tuple = (.5, .5)
    easing: str = 'smooth'

    def validate(self):
        unit_point(self.anchor, 'layer anchor')
        if self.width < 2 or self.height < 2 or min(self.fps, self.duration) <= 0:
            raise MotionError('Motion needs positive dimensions, fps and duration.')
        numbers = [self.fps, self.duration]
        for layer in ['foreground', 'background']:
            for end in ['start', 'end']:
                scale = getattr(self, f'{layer}_scale_{end}')
                shift = getattr(self, f'{layer}_shift_{end}')
                if scale <= 0 or len(shift) != 2:
                    raise MotionError('Layer scales must be positive; shifts require X Y.')
                numbers.extend([scale, *shift])
        if not np.isfinite(numbers).all() or round(self.fps*self.duration) < 2:
            raise MotionError('Motion parameters must be finite and contain at least two frames.')
        if self.easing not in ['smooth', 'linear']:
            raise MotionError('Easing must be smooth or linear.')


def reduced_motion(motion, factor):
    """At zero both layers share the initial foreground transform (no parallax)."""
    if not 0 <= factor <= 1:
        raise MotionError('Motion factor must lie in [0,1].')
    base_scale = motion.foreground_scale_start
    base_shift = np.asarray(motion.foreground_shift_start, float)
    values = {}
    for layer in ['foreground', 'background']:
        for end in ['start', 'end']:
            sk, pk = f'{layer}_scale_{end}', f'{layer}_shift_{end}'
            values[sk] = base_scale+factor*(getattr(motion, sk)-base_scale)
            values[pk] = tuple(base_shift+factor*(np.asarray(getattr(motion, pk))-base_shift))
    return replace(motion, **values)


def layer_matrix(source_size, output_size, scale, shift, anchor):
    sw, sh = source_size
    ow, oh = output_size
    factor = max(ow/sw, oh/sh)*scale
    a = np.asarray(anchor)
    translation = a*np.array([ow-1., oh-1.])+np.asarray(shift)*[ow, oh]-factor*a*[sw-1., sh-1.]
    return np.array([[factor, 0., translation[0]], [0., factor, translation[1]]], np.float32)


def transforms_at(motion, source_size, index, count, output_size=None):
    u = index/(count-1)
    if motion.easing == 'smooth':
        u = u*u*(3-2*u)
    size = output_size or (motion.width, motion.height)
    matrices = []
    for layer in ['foreground', 'background']:
        scale = (1-u)*getattr(motion, layer+'_scale_start')+u*getattr(motion, layer+'_scale_end')
        shift = ((1-u)*np.asarray(getattr(motion, layer+'_shift_start'))
                 +u*np.asarray(getattr(motion, layer+'_shift_end')))
        matrices.append(layer_matrix(source_size, size, scale, shift, motion.anchor))
    return matrices


def clean_alpha(alpha, min_component_area=0, keep_largest=False,
                fill_holes_up_to=0, close_radius=0):
    """Optional, explicit cleanup. Real holes and disconnected structures survive by default."""
    hard = np.uint8(alpha >= 128)*255
    before = int(np.count_nonzero(hard))
    if close_radius:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*close_radius+1,)*2)
        hard = cv2.morphologyEx(hard, cv2.MORPH_CLOSE, k)
    if min_component_area or keep_largest:
        n, labels, stats, _ = cv2.connectedComponentsWithStats(hard, connectivity=8)
        sizes = stats[1:, cv2.CC_STAT_AREA]
        allowed = np.where(sizes >= min_component_area)[0]+1
        if keep_largest and len(sizes):
            allowed = np.array([int(np.argmax(sizes))+1])
        hard = np.uint8(np.isin(labels, allowed))*255
    if fill_holes_up_to:
        n, labels, stats, _ = cv2.connectedComponentsWithStats(255-hard, connectivity=8)
        for i in range(1, n):
            x, y, w, h, area = stats[i]
            touches = x == 0 or y == 0 or x+w == hard.shape[1] or y+h == hard.shape[0]
            if not touches and area <= fill_holes_up_to:
                hard[labels == i] = 255
    changed = any([min_component_area, keep_largest, fill_holes_up_to, close_radius])
    result = hard if changed else alpha.copy()
    return result, dict(pixels_before=before, pixels_after=int(np.count_nonzero(result >= 128)),
        min_component_area=min_component_area, keep_largest=keep_largest,
        fill_holes_up_to=fill_holes_up_to, close_radius=close_radius,
        note='Cleanup is explicit; default preserves disconnected components and real openings.')


def exposure_budget(alpha, occlusion, motion, analysis_width=None):
    """Union every output-frame background footprint not covered by the moving foreground."""
    h, w = alpha.shape
    coarse = analysis_width is not None and w > analysis_width
    if coarse:
        factor = analysis_width/w
        sw, sh = analysis_width, max(2, round(h*factor))
        a = cv2.resize(alpha, (sw, sh), interpolation=cv2.INTER_AREA)
        occ = cv2.resize(occlusion, (sw, sh), interpolation=cv2.INTER_AREA) > 0
        ow = min(motion.width, analysis_width)
        oh = max(2, round(ow*motion.height/motion.width))
    else:
        sw, sh = w, h
        a, occ = alpha, occlusion > 0
        ow, oh = motion.width, motion.height
    required = np.zeros((sh, sw), np.uint8)
    distance = cv2.distanceTransform(np.uint8(occ), cv2.DIST_L2, 5)
    corners = np.array([[[0., 0.], [ow-1., 0.], [ow-1., oh-1.], [0., oh-1.]]], np.float32)
    count = round(motion.fps*motion.duration)
    maximum_view_fraction, minimum_margin = 0., math.inf
    for i in range(count):
        fm, bm = transforms_at(motion, (sw, sh), i, count, (ow, oh))
        inverse = cv2.invertAffineTransform(bm)
        support = cv2.transform(corners, inverse)[0]
        margin = min(support[:,0].min(), support[:,1].min(),
                     sw-1-support[:,0].max(), sh-1-support[:,1].max())
        minimum_margin = min(minimum_margin, float(margin)*w/sw)
        if margin < -.02:
            return dict(geometry_valid=False, reason='background_leaves_source',
                        minimum_background_margin=minimum_margin, sampled_frames=i+1), None, None
        near_alpha = cv2.warpAffine(a.astype(np.float32)/255, fm, (ow, oh), flags=cv2.INTER_LINEAR)
        visible = np.uint8(near_alpha < 1-1e-7)*255
        # Output -> background source. Include cubic/linear sampling support conservatively.
        needed = cv2.warpAffine(visible, inverse, (sw, sh), flags=cv2.INTER_NEAREST)
        required = cv2.bitwise_or(required, needed)
        missing_out = cv2.warpAffine(np.uint8(occ)*255, bm, (ow, oh), flags=cv2.INTER_NEAREST)/255
        maximum_view_fraction = max(maximum_view_fraction, float(np.mean(missing_out*(1-near_alpha))))
    radius = max(2, int(math.ceil(2*sw/w)))
    required = cv2.dilate(required, np.ones((2*radius+1,)*2, np.uint8))
    repair = (required > 0) & occ
    maximum_distance = float(distance[repair].max()*w/sw) if repair.any() else 0.
    data = dict(geometry_valid=True, sampled_frames=count,
        minimum_background_margin=minimum_margin,
        repair_fraction=float(np.mean(repair)), repair_pixels=int(np.count_nonzero(repair)),
        maximum_repair_distance=maximum_distance,
        maximum_repaired_view_fraction=maximum_view_fraction,
        source_size=[w,h], budget_size=[sw,sh], full_resolution=not coarse,
        sampling_guard_source_pixels=float(radius*w/sw))
    return data, np.uint8(repair)*255, required


def budget_pass(data, max_fraction, max_distance, max_view_fraction):
    return (data['geometry_valid'] and data['repair_fraction'] <= max_fraction
            and data['maximum_repair_distance'] <= max_distance
            and data['maximum_repaired_view_fraction'] <= max_view_fraction)


def render_layer_frame(source, background, alpha, motion, index):
    fm, bm = transforms_at(motion, (source.shape[1], source.shape[0]), index,
                            round(motion.fps*motion.duration))
    size = (motion.width, motion.height)
    near = cv2.warpAffine(source, fm, size, flags=cv2.INTER_CUBIC)
    far = cv2.warpAffine(background, bm, size, flags=cv2.INTER_CUBIC)
    matte = cv2.warpAffine(alpha.astype(np.float32)/255, fm, size, flags=cv2.INTER_LINEAR)[:,:,None]
    return np.uint8(np.clip(near*matte+far*(1-matte), 0, 255)+.5)


def qa_sheet(source, background, alpha, repair, validity, motion, status):
    im = Image.new('RGB', (1440, 660), '#0a1018'); d = ImageDraw.Draw(im)
    d.text((18,12),'MOTION-BOUNDED LAYERS / '+status.upper(),
           font=ImageFont.load_default(size=20),fill='#64e3ec')
    overlay = source.copy()
    overlay[alpha > 127] = np.uint8(.65*overlay[alpha > 127]+.35*np.array([220,170,30]))
    certified = background.copy()
    certified[validity == 0] = np.uint8(.45*certified[validity == 0]+.55*np.array([15,75,120]))
    certified[repair > 0] = np.uint8(.75*certified[repair > 0]+.25*np.array([220,90,25]))
    n = round(motion.fps*motion.duration)
    frames = [overlay, cv2.cvtColor(alpha,cv2.COLOR_GRAY2BGR), certified,
              render_layer_frame(source,background,alpha,motion,0),
              render_layer_frame(source,background,alpha,motion,n//2),
              render_layer_frame(source,background,alpha,motion,n-1)]
    labels = ['SOURCE / FOREGROUND OVERLAY','ALPHA / WHITE = FOREGROUND',
              'BG / AMBER = UNCERTIFIED HIDDEN CORE; CYAN = VISIBLE REPAIR',
              'APPROVED MOTION / FIRST','APPROVED MOTION / MIDDLE','APPROVED MOTION / LAST']
    for k,(fr,label) in enumerate(zip(frames,labels)):
        tile = Image.fromarray(cv2.cvtColor(fr,cv2.COLOR_BGR2RGB));tile.thumbnail((480,270))
        x,y=(k%3)*480,55+(k//3)*300
        im.paste(tile,(x+(480-tile.width)//2,y+(270-tile.height)//2))
        d.text((x+8,y+276),label,font=ImageFont.load_default(size=10),fill='#d1e0e5')
    return im


def prepare_layers(input_path, output_dir, *, motion=None, roi=None, alpha_path=None,
                   foreground_points=(), background_points=(), backend='telea',
                   working_width=1920, analysis_width=960, iterations=5, feather=.7,
                   mask_expand=2, min_component_area=0, keep_largest=False,
                   fill_holes_up_to=0, close_radius=0, inpaint_radius=3.,
                   max_repair_fraction=.035, max_fill_distance=24., max_view_fraction=.12,
                   strict_motion=False, min_motion_factor=.05, ffmpeg=None, overwrite=False):
    """Return a manifest; rejected motion writes only a status report, never certified assets.

    ``approved_motion`` has flat underscore keys matching the shared layered
    renderer. Always consume these values; a reduced request is never silent.
    """
    cv2.setNumThreads(2)
    motion = motion or LayerMotion()
    motion.validate()
    input_path, output_dir = Path(input_path), Path(output_dir)
    ffmpeg = ffmpeg or shutil.which('ffmpeg')
    if backend not in ['telea','ns']:
        raise MotionError('Inpainting backend must be telea or ns.')
    positive = [max_repair_fraction, max_fill_distance, max_view_fraction, inpaint_radius]
    if (not np.isfinite(positive).all() or min(positive) <= 0
            or not 0 < min_motion_factor <= 1 or feather < 0 or mask_expand < 0
            or max_repair_fraction > 1 or max_view_fraction > 1
            or not np.isfinite([feather,mask_expand,working_width,analysis_width,
                               min_component_area,fill_holes_up_to,close_radius]).all()
            or working_width < 0 or analysis_width < 32 or not 1 <= iterations <= 50
            or min(min_component_area,fill_holes_up_to,close_radius) < 0):
        raise MotionError('Invalid cleanup, inpainting, or motion budget values.')
    known_outputs = ['foreground.png','background.png','alpha.png','repair-mask.png',
                     'required-background-mask.png','background-validity.png','qa-contact.jpg','manifest.json']
    if output_dir.is_symlink() or (output_dir.exists() and not output_dir.is_dir()):
        raise MotionError('Layer output directory must be an ordinary directory.')
    inputs = [input_path] + ([Path(alpha_path)] if alpha_path is not None else [])
    outputs = [output_dir/name for name in known_outputs]
    for destination in outputs:
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise MotionError('Layer output targets must be ordinary files, not symlinks or directories.')
        for source_path in inputs:
            if (destination.resolve() == source_path.resolve()
                    or (destination.exists() and source_path.exists() and destination.samefile(source_path))):
                raise MotionError('Layer outputs must not replace or alias the input photograph or supplied alpha.')
    existing = [path for path in outputs if path.exists()]
    for i, destination in enumerate(existing):
        if any(destination.samefile(other) for other in existing[i+1:]):
            raise MotionError('Layer output targets must not be hard-link aliases of each other.')
    if any((output_dir/name).exists() for name in known_outputs) and not overwrite:
        raise MotionError('Layer output already exists; explicitly use --overwrite.')
    if ffmpeg and media_metadata(ffmpeg,input_path)['hdr']:
        raise MotionError('Tagged HDR input requires explicit SDR conversion first.')
    full = cv2.imread(str(input_path),cv2.IMREAD_UNCHANGED)
    if full is None or full.dtype != np.uint8 or full.ndim != 3 or full.shape[2] not in [3,4]:
        raise MotionError('Input must be a readable 8-bit SDR RGB/RGBA still photograph.')
    original_size = [full.shape[1],full.shape[0]]
    if full.shape[2] == 4 and np.any(full[:,:,3] != 255):
        raise MotionError('A transparent cutout is not an original scene photo; provide the opaque scene plus --alpha.')
    original_alpha = None
    full = full[:,:,:3]
    factor = min(1.,working_width/full.shape[1]) if working_width else 1.
    size = (round(full.shape[1]*factor),round(full.shape[0]*factor))
    source = cv2.resize(full,size,interpolation=cv2.INTER_AREA) if factor<1 else full.copy()
    if alpha_path is not None:
        original_alpha = cv2.imread(str(alpha_path),cv2.IMREAD_UNCHANGED)
        if (original_alpha is None or original_alpha.dtype != np.uint8
                or original_alpha.ndim != 2 or list(original_alpha.shape[::-1]) != original_size):
            raise MotionError('Supplied alpha must be 8-bit grayscale at the original photograph dimensions.')
        segmentation = dict(method='supplied alpha',input_name=Path(alpha_path).name)
    else:
        if roi is None:
            raise MotionError('Provide --roi and optional seeds, or an existing --alpha.')
        alpha,segmentation = static_grabcut(source,roi,foreground_points,background_points,
                                           iterations,min(analysis_width,size[0]),0.)
    if original_alpha is not None:
        alpha = cv2.resize(original_alpha,size,interpolation=cv2.INTER_AREA) if factor<1 else original_alpha.copy()
    alpha,cleanup = clean_alpha(alpha,min_component_area,keep_largest,fill_holes_up_to,close_radius)
    if feather:
        alpha = cv2.GaussianBlur(alpha,(0,0),feather)
    fraction = float(np.mean(alpha>=128))
    if not .0001 < fraction < .98:
        raise MotionError('Foreground is empty/nearly full; refine the ROI, seeds, alpha, or cleanup settings.')
    occlusion = np.uint8(alpha>2)*255
    if mask_expand:
        k=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*mask_expand+1,)*2)
        occlusion=cv2.dilate(occlusion,k)
    budget_limits=dict(maximum_repair_fraction=max_repair_fraction,
        maximum_fill_distance_working_pixels=max_fill_distance,
        maximum_repaired_view_fraction=max_view_fraction,
        min_motion_factor=min_motion_factor,
        interpretation='Geometric limits for classical inpainting; not a semantic completion-quality guarantee.')
    report=dict(schema_version=1,input_name=input_path.name,
        status_scope='Motion geometry and configured repair-size budgets; accepted does not waive visual review.',
        input_sha256=hashlib.sha256(input_path.read_bytes()).hexdigest(),
        original_size=original_size,working_size=list(size),working_scale=factor,
        input_validation=dict(hdr_tags_checked=bool(ffmpeg),
            note='8-bit RGB is required; absence of HDR tags does not prove an intended SDR transfer function.'),
        segmentation=segmentation,cleanup=cleanup,foreground_fraction=fraction,
        backend=backend,requested_motion=asdict(motion),budget_limits=budget_limits,
        method='ROI/alpha foreground + actual classical inpainting + full-motion visibility budget',
        limits=['Single-view limited 2.5D; no scene reconstruction, depth network, or recovered hidden object side.',
                'Inpainted hidden core is not certified as a real or aesthetically accurate background.',
                'Consume approved_motion and background-validity.png; other motions may reveal untrusted content.'])
    attempts=[]
    requested_data,_,_=exposure_budget(alpha,occlusion,motion,640)
    factors=[1.] if strict_motion else [1.,.75,.5,.35,.25,.18,.125,.09,.0625,.05]
    factors=[x for x in factors if x>=min_motion_factor-1e-9]
    if not strict_motion and (not factors or factors[-1]>min_motion_factor+1e-9):
        factors.append(min_motion_factor)
    approved=selected_data=repair=required=None
    selected_factor=None
    for amount in factors:
        candidate=reduced_motion(motion,amount)
        coarse,_,_=exposure_budget(alpha,occlusion,candidate,640)
        attempts.append(dict(motion_factor=amount,stage='coarse',budget=coarse))
        # Coarse area resampling can close narrow known-background openings and
        # exaggerate distance-to-context. It is a diagnostic, never a rejection
        # authority; every candidate gets the actual working-resolution test.
        full_data,full_repair,full_required=exposure_budget(alpha,occlusion,candidate)
        attempts.append(dict(motion_factor=amount,stage='full_resolution',budget=full_data))
        if budget_pass(full_data,max_repair_fraction,max_fill_distance,max_view_fraction):
            approved,selected_data,repair,required=candidate,full_data,full_repair,full_required
            selected_factor=amount
            break
    output_dir.parent.mkdir(parents=True,exist_ok=True)
    if approved is None:
        report.update(status='rejected',motion_factor=None,approved_motion=None,
            requested_budget=requested_data,budget_attempts=attempts,
            recovery=['Reduce relative foreground/background movement or scale difference.',
                      'Supply genuine background pixels from an unobstructed reference view.',
                      'Refine an incorrect alpha rather than increasing blur or claiming hidden geometry.'])
        if existing:
            raise MotionError('Requested motion was rejected; existing layer assets and manifest were preserved. '
                              'Use a new output directory for a standalone rejection report.')
        output_dir.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.motion-layers-',dir=str(output_dir.parent)) as tmp:
            temp=Path(tmp)/'manifest.json';temp.write_text(json.dumps(report,indent=2)+'\n')
            publish_bundle([(temp,output_dir/'manifest.json')])
        return report
    # Inpaint with the ENTIRE occluder removed: repairing only a thin interior
    # strip would wrongly use the still-visible foreground as fill context.
    # Only the motion-certified visible band is trusted, not the large hidden core.
    method=cv2.INPAINT_TELEA if backend=='telea' else cv2.INPAINT_NS
    background=cv2.inpaint(source,occlusion,inpaint_radius,method)
    validity=np.uint8((occlusion==0)|(repair>0))*255
    unseen=(required>0)&(validity==0)
    assert not unseen.any()
    gray=cv2.cvtColor(background,cv2.COLOR_BGR2GRAY)
    lap=np.abs(cv2.Laplacian(gray,cv2.CV_32F))
    ring=(cv2.dilate(occlusion,np.ones((11,11),np.uint8))>0)&(occlusion==0)
    detail_repair=float(np.mean(lap[repair>0])) if np.any(repair) else None
    detail_ring=float(np.mean(lap[ring])) if ring.any() else None
    warnings=[]
    if selected_factor<1:
        warnings.append('REQUESTED MOTION WAS REDUCED; render approved_motion, not the original requested values.')
    if detail_repair is not None and detail_ring and detail_repair<.15*detail_ring:
        warnings.append('Visible repair is substantially smoother than the surrounding background; visually inspect the exposed strip.')
    report.update(status='motion_reduced' if selected_factor<1 else 'accepted',
        motion_factor=selected_factor,approved_motion=asdict(approved),
        requested_budget=requested_data,approved_budget=selected_data,budget_attempts=attempts,
        inpainting=dict(backend=backend,radius_working_pixels=inpaint_radius,
            occlusion_pixels=int(np.count_nonzero(occlusion)),
            certified_visible_repair_pixels=int(np.count_nonzero(repair)),
            hidden_core_pixels_not_certified=int(np.count_nonzero((occlusion>0)&(repair==0)))),
        qa=dict(review_required=bool(np.any(repair)),
            visible_repair_laplacian_mean=detail_repair,known_ring_laplacian_mean=detail_ring,
            note='Texture statistics are diagnostics, not proof of realistic completion. Inspect the contact sheet and rendered motion.'),
        warnings=warnings,assets={name:name for name in known_outputs if name!='manifest.json'})
    with tempfile.TemporaryDirectory(prefix='.motion-layers-',dir=str(output_dir.parent)) as tmp:
        stage=Path(tmp)
        images={'foreground.png':source,'background.png':background,'alpha.png':alpha,
                'repair-mask.png':repair,'required-background-mask.png':required,
                'background-validity.png':validity}
        for name,image in images.items():
            if not cv2.imwrite(str(stage/name),image):
                raise MotionError('Layer PNG export failed.')
        qa_sheet(source,background,alpha,repair,validity,approved,report['status']).save(stage/'qa-contact.jpg',quality=94)
        for name in images:
            if cv2.imread(str(stage/name),cv2.IMREAD_UNCHANGED).shape[:2]!=source.shape[:2]:
                raise MotionError('Layer output dimensions changed during encoding.')
        (stage/'manifest.json').write_text(json.dumps(report,indent=2)+'\n')
        output_dir.mkdir(exist_ok=True)
        publish_bundle([(stage/name,output_dir/name) for name in known_outputs])
    return report


def build_parser():
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--input',type=Path,required=True)
    p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--roi',type=float,nargs=4)
    p.add_argument('--alpha',type=Path)
    p.add_argument('--foreground-point',dest='foreground_points',type=float,nargs=2,action='append',default=[])
    p.add_argument('--background-point',dest='background_points',type=float,nargs=2,action='append',default=[])
    p.add_argument('--backend',choices=['telea','ns'],default='telea')
    p.add_argument('--working-width',type=int,default=1920,help='Downscale RGB/alpha consistently to this width; 0 keeps native dimensions.')
    p.add_argument('--analysis-width',type=int,default=960)
    p.add_argument('--iterations',type=int,default=5)
    p.add_argument('--feather',type=float,default=.7,help='Alpha Gaussian sigma in working-source pixels.')
    p.add_argument('--mask-expand',type=int,default=2,help='Expand the occluder for safe background color context.')
    p.add_argument('--min-component-area',type=int,default=0,help='Explicitly remove foreground components smaller than this working-pixel area; default preserves them.')
    p.add_argument('--keep-largest',action='store_true')
    p.add_argument('--fill-holes-up-to',type=int,default=0,help='Fill only enclosed mask holes up to this area; default preserves real openings.')
    p.add_argument('--close-radius',type=int,default=0)
    p.add_argument('--inpaint-radius',type=float,default=3.)
    p.add_argument('--max-repair-fraction',type=float,default=.035)
    p.add_argument('--max-fill-distance',type=float,default=24.,help='Maximum certified repair distance from known background, in working-source pixels.')
    p.add_argument('--max-view-fraction',type=float,default=.12)
    p.add_argument('--min-motion-factor',type=float,default=.05)
    p.add_argument('--strict-motion',action='store_true',help='Reject an unsupported requested motion instead of reducing it.')
    p.add_argument('--ffmpeg',default=shutil.which('ffmpeg'))
    p.add_argument('--overwrite',action='store_true')
    for key,default,kind in [('width',1280,int),('height',720,int),('fps',30.,float),('duration',4.,float)]:
        p.add_argument('--'+key,type=kind,default=default)
    p.add_argument('--anchor',type=float,nargs=2,default=[.5,.5])
    p.add_argument('--easing',choices=['smooth','linear'],default='smooth')
    for layer in ['foreground','background']:
        p.add_argument('--'+layer+'-scale-start',type=float,default=1.2)
        p.add_argument('--'+layer+'-scale-end',type=float,default=1.2 if layer=='foreground' else 1.05)
        p.add_argument('--'+layer+'-shift-start',type=float,nargs=2,default=[0.,0.])
        p.add_argument('--'+layer+'-shift-end',type=float,nargs=2,default=[0.,0.])
    return p


def main():
    p=build_parser();a=p.parse_args()
    keys=set(LayerMotion.__dataclass_fields__)
    motion=LayerMotion(**{k:getattr(a,k) for k in keys})
    options={k:v for k,v in vars(a).items() if k not in keys|{'input','output_dir','alpha'}}
    try:
        report=prepare_layers(a.input,a.output_dir,motion=motion,alpha_path=a.alpha,**options)
    except (MotionError,OSError,cv2.error,ValueError) as exc:
        p.exit(2,'Motion layers: '+str(exc)+'\n')
    result=dict(status=report['status'],manifest=str(a.output_dir/'manifest.json'),
                motion_factor=report['motion_factor'],approved_motion=report['approved_motion'])
    print(json.dumps(result))
    if report['status']=='rejected':
        p.exit(2,'Requested motion exceeds the bounded inpainting support; see manifest recovery suggestions.\n')


if __name__=='__main__':main()
