"""Limited 2.5D vertigo-style composite; never a claim of a real dolly zoom.

The rigid museum card remains fixed while a completed distant plate changes
scale. Source assets include approximated, previously occluded water pixels.
"""
from pathlib import Path
import argparse
import json
import shutil
import subprocess

import cv2
import numpy as np


def grade(image):
    xx = [0, 24, 64, 128, 192, 255]
    yy = [0, 20, 65, 139, 205, 255]
    lut = np.interp(np.arange(256), xx, yy).astype(np.uint8)
    return cv2.LUT(image, lut)


def transform(scale, anchor, destination):
    return np.array([[scale, 0., destination[0] - scale * anchor[0]],
                     [0., scale, destination[1] - scale * anchor[1]]], np.float32)


def support_margin(matrix, source_size, destination_size):
    sw, sh = source_size
    dw, dh = destination_size
    inverse = cv2.invertAffineTransform(matrix)
    corners = cv2.transform(np.array([[[0., 0.], [dw-1., 0.],
                                      [dw-1., dh-1.], [0., dh-1.]]], np.float32), inverse)[0]
    return float(min(corners[:, 0].min(), corners[:, 1].min(),
                     sw-1-corners[:, 0].max(), sh-1-corners[:, 1].max()))


def museum_roof_constraint(mask):
    # The shadowed backs of the white petals are gray-green like the water.
    # A hand-checked closed roof contour protects those rigid architectural faces.
    outline = np.array([
        [844,385],[852,412],[840,464],[810,510],[860,478],[872,472],
        [871,489],[856,539],[825,574],[847,571],[855,565],[855,585],
        [840,613],[821,639],[806,657],[765,675],[740,682],[694,686],
        [676,674],[661,682],[607,684],[584,671],[566,668],[549,653],
        [521,650],[500,630],[475,605],[473,588],[485,571],[500,563],
        [500,540],[513,516],[529,507],[544,503],[546,485],[565,476],
        [585,471],[602,462],[617,465],[621,460],[647,466],[670,469],
        [690,465],[715,458],[737,446],[758,427],[780,399],[800,383],
        [813,381],[825,382]],dtype=np.float32)
    points = (outline-np.array([640.,500.]))/.9+np.array([1080.,850.])
    cv2.fillPoly(mask,[np.round(points).astype(np.int32)],255)
    return mask


def refine_mask(source, museum, near):
    """Refine the loose old card along the actual white roof and pink coastline."""
    yy = np.indices(museum.shape)[0]
    hue, saturation, value = cv2.split(cv2.cvtColor(source,cv2.COLOR_BGR2HSV))
    roi = np.maximum(museum,near)>0
    inside = cv2.erode(museum,np.ones((29,29),np.uint8))>0
    white = (saturation<90)&(value>120)&(yy>650)&(yy<1030)&(museum>0)
    land = inside&(yy>970)&(((saturation>100)&(value>62))|(value>135))
    water = (hue>28)&(hue<97)&(saturation<105)&(value<117)
    gc = np.full(museum.shape,cv2.GC_BGD,np.uint8)
    gc[cv2.dilate(np.uint8(roi),np.ones((9,9),np.uint8))>0]=cv2.GC_PR_BGD
    gc[roi]=cv2.GC_PR_FGD
    gc[white|land]=cv2.GC_FGD
    gc[water&roi]=cv2.GC_BGD
    gc[(near>0)&(~water)]=cv2.GC_FGD
    bg,fg=np.zeros((1,65),np.float64),np.zeros((1,65),np.float64)
    cv2.grabCut(source,gc,None,bg,fg,3,cv2.GC_INIT_WITH_MASK)
    mask=np.uint8(np.isin(gc,[cv2.GC_FGD,cv2.GC_PR_FGD]))*255
    # Preserve each connected silhouette as a rigid card; dark roof/grass
    # patches inside it must not become moving holes in the foreground.
    contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    filled=np.zeros_like(mask)
    for contour in contours:
        if cv2.contourArea(contour)>1000:
            cv2.drawContours(filled,[contour],0,255,cv2.FILLED)
    return museum_roof_constraint(filled)


def render(args):
    cv2.setNumThreads(2)
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    if args.input_dir:
        source = cv2.imread(str(args.input_dir/'source.jpg'))
        background = cv2.imread(str(args.input_dir/'background.jpg'))
        combined = cv2.imread(str(args.input_dir/'foreground_alpha.png'), cv2.IMREAD_GRAYSCALE)
        if any(a is None for a in [source, background, combined]):
            raise FileNotFoundError('Input directory requires source.jpg, background.jpg and foreground_alpha.png')
    else:
        source_full = cv2.imread(str(args.source))
        background = cv2.imread(str(args.background))
        museum = cv2.imread(str(args.museum_mask), cv2.IMREAD_GRAYSCALE)
        near = cv2.imread(str(args.near_mask), cv2.IMREAD_GRAYSCALE)
        if any(a is None for a in [source_full, background, museum, near]):
            raise FileNotFoundError('A source, background plate, or layer mask is missing')
        source = source_full[115:1330].copy()
        assert museum.shape == near.shape == source.shape[:2]
        combined = refine_mask(source,museum,near)
        assets = out/'assets'
        assets.mkdir(exist_ok=True)
        cv2.imwrite(str(assets/'source.jpg'),source,[cv2.IMWRITE_JPEG_QUALITY,98])
        cv2.imwrite(str(assets/'background.jpg'),background,[cv2.IMWRITE_JPEG_QUALITY,98])
        cv2.imwrite(str(assets/'foreground_alpha.png'),combined)
        (assets/'parameters.json').write_text(json.dumps(dict(
            anchor=[1080,850],destination_at_1280=[640,500],foreground_scale=.9,
            background_scale=[.90,.68],source_size=[2160,1215],
            method='Rigid foreground + completed far plate; limited 2.5D counter-scale composite.'),indent=2))
    assert source.shape == background.shape
    assert combined.shape == source.shape[:2]
    source = grade(source)
    background = grade(background)
    sh, sw = source.shape[:2]
    w, h = args.width, round(args.width * 9 / 16)
    if h % 2:
        h += 1
    factor = w / 1280
    fps, count = 30, 120
    anchor = np.array([1080., 850.])
    destination = np.array([640., 500.]) * factor
    fg_matrix = transform(.9 * factor, anchor, destination)
    alpha_source = cv2.GaussianBlur(combined.astype(np.float32) / 255,
                                    (0, 0), .7)
    alpha = cv2.warpAffine(alpha_source, fg_matrix, (w, h), flags=cv2.INTER_LINEAR,
                           borderMode=cv2.BORDER_CONSTANT)[..., None]
    foreground = cv2.warpAffine(source, fg_matrix, (w, h), flags=cv2.INTER_CUBIC,
                                borderMode=cv2.BORDER_CONSTANT)
    # All foreground is rigid: no depth-dependent bending inside the museum.
    output = out / 'layered_vertigo_4s.mp4'
    ffmpeg = args.ffmpeg or shutil.which('ffmpeg')
    if ffmpeg is None:
        raise RuntimeError('Supply --ffmpeg or put ffmpeg on PATH')
    command = [str(ffmpeg), '-hide_banner', '-y', '-v', 'error',
               '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{w}x{h}',
               '-r', str(fps), '-i', 'pipe:0', '-an',
               '-vf', 'scale=in_range=pc:out_range=tv:out_color_matrix=bt709,format=yuv420p',
               '-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-threads', '2',
               '-color_primaries', 'bt709', '-color_trc', 'bt709',
               '-colorspace', 'bt709', '-color_range', 'tv',
               '-movflags', '+faststart', str(output)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    records, samples = [], []
    for i in range(count):
        u = i / (count - 1)
        ease = u*u*(3-2*u)
        # A fixed near card masks the smaller hidden region of the far plate.
        bg_scale = (.90 - .22 * ease) * factor
        matrix = transform(bg_scale, anchor, destination)
        margin = support_margin(matrix, (sw, sh), (w, h))
        assert margin >= 8., (i, margin)
        far = cv2.warpAffine(background, matrix, (w, h), flags=cv2.INTER_CUBIC,
                             borderMode=cv2.BORDER_CONSTANT)
        frame = np.uint8(np.clip(far * (1-alpha) + foreground * alpha, 0, 255) + .5)
        process.stdin.write(frame.tobytes())
        if i in [0, 30, 60, 90, count-1]:
            cv2.imwrite(str(out/f'layered_vertigo_{i:03d}.jpg'), frame,
                         [cv2.IMWRITE_JPEG_QUALITY, 95])
            samples.append(cv2.resize(frame, (640, 360)))
        records.append(dict(frame=i, background_scale=float(bg_scale/factor),
                            foreground_scale=.9, source_pixel_margin=margin))
    process.stdin.close()
    if process.wait():
        raise RuntimeError('Video encoding failed')
    cv2.imwrite(str(out/'layered_vertigo_contact.jpg'), np.concatenate(samples, axis=1),
                 [cv2.IMWRITE_JPEG_QUALITY, 95])
    cap = cv2.VideoCapture(str(output))
    assert round(cap.get(cv2.CAP_PROP_FPS)) == fps
    decoded, diffs, prev, black = 0, [], None, []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        small = cv2.resize(frame, (320, 180))
        if np.mean(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) < 8) > .995:
            black.append(decoded)
        if prev is not None:
            diffs.append(float(np.mean(cv2.absdiff(small, prev))))
        prev = small
        decoded += 1
    cap.release()
    assert decoded == count and not black
    # The foreground card uses exactly the same affine transform every frame.
    report = dict(title='LAYERED VERTIGO', subtitle='2.5D合成',
                  output=str(output), fps=fps, frames=count, duration_seconds=4,
                  size=[w, h], sources={
                    'input_dir':str(args.input_dir) if args.input_dir else str(out/'assets'),
                    'original_photo':'IMG_0434', 'source':'source.jpg',
                    'background':'background.jpg','alpha':'foreground_alpha.png'},
                  method='Fixed rigid museum/peninsula/near-roof card, counter-scaled distant plate.',
                  foreground_size_change_percent=0.,
                  background_scale_change_percent=100*(.68/.90-1),
                  min_source_pixel_margin=min(r['source_pixel_margin'] for r in records),
                  black_frames=black, decoded_frames=decoded,
                  frame_difference_min=min(diffs), frame_difference_median=float(np.median(diffs)),
                  limitations=[
                    'Single-view 2.5D composite, not an actual camera dolly zoom or a metric 3D reconstruction.',
                    'Previously occluded water pixels use existing nearest-background/Telea completion.',
                    'The foreground silhouette includes its peninsula and nearby roof to preserve attachments.',
                    'Scale change is intentionally limited; do not expand it into an orbit or reveal hidden sides.'],
                  trajectory=records)
    (out/'layered_vertigo_manifest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({k:v for k,v in report.items() if k != 'trajectory'},ensure_ascii=False,indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    depth = Path('.vlog_v2/depth')
    parser.add_argument('--source', type=Path, default=depth/'IMG_0434_source.jpg')
    parser.add_argument('--input-dir', type=Path,
                        help='Portable asset folder: source.jpg, background.jpg, foreground_alpha.png')
    parser.add_argument('--background', type=Path, default=depth/'IMG_0434_completed_background.jpg')
    parser.add_argument('--museum-mask', type=Path, default=depth/'IMG_0434_artscience_museum_and_peninsula_mask.png')
    parser.add_argument('--near-mask', type=Path, default=depth/'IMG_0434_near_roof_and_terrace_mask.png')
    parser.add_argument('--output-dir', type=Path, default=Path('.vlog_showcase/vertigo'))
    parser.add_argument('--ffmpeg', type=Path)
    parser.add_argument('--width', type=int, default=1280)
    render(parser.parse_args())
