"""Portable, bounded image geometry and streaming video I/O for camera effects."""
from pathlib import Path
import math
import re
import subprocess
import tempfile

import cv2
import numpy as np


class MotionError(ValueError):
    """An input or requested motion cannot be supported without inventing pixels."""


def unit_point(value, label='point'):
    point = np.asarray(value, dtype=float)
    if point.shape != (2,) or not np.isfinite(point).all() or np.any(point < 0) or np.any(point > 1):
        raise MotionError(f'{label} requires two normalized coordinates in [0, 1].')
    return point


def ease(value, mode='smooth'):
    x = float(np.clip(value, 0, 1))
    return x*x*(3-2*x) if mode == 'smooth' else x


def mix(a, b, t):
    return np.asarray(a)*(1-t)+np.asarray(b)*t


def media_metadata(ffmpeg, path):
    result = subprocess.run([str(ffmpeg), '-hide_banner', '-i', str(path)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    text = result.stderr
    return dict(hdr=bool(re.search(r'arib-std-b67|smpte2084|DOVI configuration', text, re.I)),
                audio=bool(re.search(r'Stream .*Audio:', text)))


class Reader:
    """CFR frame-index access, retaining only the latest decoded source frame."""
    def __init__(self, path, ffmpeg):
        self.path = Path(path)
        if not self.path.is_file():
            raise MotionError(f'Input file does not exist: {self.path}')
        self.metadata = media_metadata(ffmpeg, self.path)
        if self.metadata['hdr']:
            raise MotionError('HDR input detected. Run the convert-hdr subcommand first; '
                              'ordinary effects accept SDR and never silently tone-map it.')
        self.still = self.path.suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff'}
        self.cap = None
        if self.still:
            image = cv2.imread(str(self.path), cv2.IMREAD_UNCHANGED)
            if image is None or image.dtype != np.uint8:
                raise MotionError('A still input must be a readable 8-bit SDR image.')
            if image.ndim == 2:
                image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
            elif image.shape[2] == 4:
                # A standalone transparent image has no defined scene behind it.
                if np.any(image[:, :, 3] != 255):
                    raise MotionError('For transparency use the explicit foreground/background/alpha layer commands.')
                image = image[:, :, :3]
            self.cached = image
            self.fps, self.count, self.duration, self.index = 0., 1, math.inf, 0
        else:
            self.cap = cv2.VideoCapture(str(self.path))
            self.fps = float(self.cap.get(cv2.CAP_PROP_FPS))
            self.count = int(round(self.cap.get(cv2.CAP_PROP_FRAME_COUNT)))
            ok, self.cached = self.cap.read()
            if not ok or not np.isfinite(self.fps) or self.fps <= 0 or self.count < 1:
                self.close()
                raise MotionError(f'Cannot determine a video frame timeline: {self.path}')
            self.duration, self.index = self.count/self.fps, 0
        self.height, self.width = self.cached.shape[:2]
        self.repeated_requests = 0

    def frame_index(self, index):
        if self.still:
            return self.cached
        index = int(index)
        if not 0 <= index < self.count:
            raise MotionError(f'Source frame {index} outside [0, {self.count-1}] in {self.path.name}.')
        if index == self.index:
            self.repeated_requests += 1
            return self.cached
        if index < self.index:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, index)
            self.index = index-1
        while self.index < index-1:
            if not self.cap.grab():
                raise MotionError('Source ended while selecting a frame.')
            self.index += 1
        ok, self.cached = self.cap.read()
        if not ok:
            raise MotionError('Source ended while decoding a frame.')
        self.index = index
        return self.cached

    def at(self, seconds):
        if seconds < 0:
            raise MotionError('Source time must be nonnegative.')
        if self.still:
            return self.cached
        return self.frame_index(int(math.floor(seconds*self.fps+1e-7)))

    def check_range(self, start, end):
        if start < 0 or end < start or end > self.duration+1e-5:
            raise MotionError(f'Requested source interval [{start:.3f}, {end:.3f}) is unavailable; '
                              f'{self.path.name} has {self.duration:.3f} seconds.')

    def close(self):
        if self.cap is not None:
            self.cap.release()


def crop_matrix(source_size, output_size, zoom=1., center=(.5, .5), angle=0., bounds='error'):
    """Return output-to-source affine mapping and its sampling diagnostics."""
    sw, sh = source_size
    ow, oh = output_size
    center = unit_point(center, 'crop center')
    if not np.isfinite([zoom, angle]).all() or zoom <= 0:
        raise MotionError('Zoom must be positive and angles finite.')
    step = min(sw/ow, sh/oh)/zoom
    rad = math.radians(angle)
    linear = step*np.array([[math.cos(rad), math.sin(rad)], [-math.sin(rad), math.cos(rad)]])
    corners = np.array([[-(ow-1)/2, -(oh-1)/2], [(ow-1)/2, -(oh-1)/2],
                        [(ow-1)/2, (oh-1)/2], [-(ow-1)/2, (oh-1)/2]])
    offsets = corners @ linear.T
    half = np.max(np.abs(offsets), axis=0)
    limit = np.array([sw-1., sh-1.])
    if np.any(2*half > limit+1e-7):
        raise MotionError('Rotated output footprint exceeds the source; increase zoom or enable roll auto-fit.')
    requested = center*limit
    actual = np.clip(requested, half, limit-half)
    if bounds == 'error' and np.max(np.abs(actual-requested)) > 1e-5:
        raise MotionError('Crop path crosses a source edge; adjust center/zoom or explicitly use --bounds clamp.')
    translation = actual-linear @ np.array([(ow-1)/2, (oh-1)/2])
    matrix = np.column_stack([linear, translation]).astype(np.float32)
    pts = offsets+actual
    margin = float(min(pts[:, 0].min(), pts[:, 1].min(),
                       sw-1-pts[:, 0].max(), sh-1-pts[:, 1].max()))
    return matrix, dict(source_edge_margin=max(0., margin),
                        center_clamped=bool(np.max(abs(actual-requested)) > 1e-5),
                        actual_center=(actual/limit).tolist(),
                        output_pixels_per_source_pixel=float(1/step))


def rotation_fit_zoom(source_size, output_size, angle, center=(.5, .5)):
    sw, sh = source_size
    ow, oh = output_size
    center = unit_point(center, 'roll center')*np.array([sw-1, sh-1])
    space = np.minimum(center, np.array([sw-1, sh-1])-center)
    if np.min(space) <= 0:
        raise MotionError('A rotation center exactly on a source edge has no finite safe crop.')
    t = math.radians(angle)
    step = min(sw/ow, sh/oh)
    half = step*np.array([abs(math.cos(t))*(ow-1)+abs(math.sin(t))*(oh-1),
                          abs(math.sin(t))*(ow-1)+abs(math.cos(t))*(oh-1)])/2
    return float(np.max(half/space))*1.005


class Camera:
    def __init__(self, size, bounds='error', max_upscale=8.):
        self.size, self.bounds, self.max_upscale = size, bounds, max_upscale
        self.diagnostics = []

    def draw(self, source, zoom=1., center=(.5, .5), angle=0.):
        h, w = source.shape[:2]
        matrix, data = crop_matrix((w, h), self.size, zoom, center, angle, self.bounds)
        if data['output_pixels_per_source_pixel'] > self.max_upscale:
            raise MotionError('Requested enlargement exceeds --max-upscale.')
        self.diagnostics.append(data)
        return cv2.warpAffine(source, matrix, self.size,
                             flags=cv2.INTER_CUBIC | cv2.WARP_INVERSE_MAP,
                             borderMode=cv2.BORDER_CONSTANT)

    def summary(self):
        d = self.diagnostics
        return dict(samples=len(d), minimum_source_edge_margin=min((x['source_edge_margin'] for x in d), default=None),
                    clamped_samples=sum(x['center_clamped'] for x in d),
                    maximum_output_pixels_per_source_pixel=max((x['output_pixels_per_source_pixel'] for x in d), default=None))


def normalized_speed_map(count, span, points):
    if count < 2 or span <= 0:
        raise MotionError('A speed ramp needs at least two output frames and positive source duration.')
    try:
        pts = np.asarray(points, dtype=float)
    except (TypeError, ValueError) as exc:
        raise MotionError('Each speed point needs exactly a fraction and a relative speed.') from exc
    if (pts.ndim != 2 or pts.shape[1] != 2 or len(pts) < 2 or not np.isfinite(pts).all()
            or abs(pts[0, 0]) > 1e-9 or abs(pts[-1, 0]-1) > 1e-9
            or np.any(np.diff(pts[:, 0]) <= 0) or np.any(pts[:, 1] <= 0)):
        raise MotionError('Speed points must have increasing fractions from 0 to 1 and positive weights.')
    x = np.linspace(0, 1, count)
    weights = np.interp(x, pts[:, 0], pts[:, 1])
    cumulative = np.r_[0., np.cumsum((weights[:-1]+weights[1:])/2)]
    scale = span/cumulative[-1]
    return cumulative*scale, weights*scale


def mask_path(directory, pattern, index):
    try:
        name = pattern.format(index=int(index))
    except (ValueError, KeyError, IndexError) as exc:
        raise MotionError('Mask pattern must use {index}, for example {index:04d}.png.') from exc
    root = Path(directory).resolve()
    path = (root/name).resolve()
    if root not in path.parents:
        raise MotionError('A mask pattern must remain inside its mask directory.')
    if not path.is_file():
        raise MotionError(f'Missing mask frame: {path.name}')
    return path


def transition_mask_indices(count, output_fps, start, end, mask_fps, offset=0):
    """Map a half-open transition to masks, tolerating floating frame-boundary noise."""
    return [offset+int(math.floor((i/output_fps-start)*mask_fps+1e-7))
            for i in range(count) if start-1e-7 <= i/output_fps < end-1e-7]


def write_video(ffmpeg, path, frames, size, fps, expected_count, crf=20):
    """Encode to a temporary file; stderr goes to disk so pipe output cannot deadlock."""
    with tempfile.TemporaryFile() as log:
        cmd = [str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-y', '-f', 'rawvideo',
               '-pix_fmt', 'bgr24', '-s', f'{size[0]}x{size[1]}', '-r', str(fps), '-i', 'pipe:0',
               '-an', '-vf', 'scale=in_range=pc:out_range=tv:out_color_matrix=bt709,format=yuv420p,setsar=1',
               '-c:v', 'libx264', '-preset', 'fast', '-crf', str(crf), '-threads', '2',
               '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709',
               '-color_range', 'tv', '-map_metadata', '-1', '-movflags', '+faststart', str(path)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log)
        count = 0
        try:
            for frame in frames:
                if frame.dtype != np.uint8 or frame.shape != (size[1], size[0], 3):
                    raise MotionError('A rendered frame has an unexpected size or pixel format.')
                proc.stdin.write(np.ascontiguousarray(frame).tobytes())
                count += 1
            proc.stdin.close()
            code = proc.wait()
            if code:
                log.seek(0)
                raise MotionError(log.read(16000).decode(errors='replace'))
            if count != expected_count:
                raise MotionError(f'Rendered {count} frames; expected {expected_count}.')
        except BaseException:
            if proc.poll() is None:
                try:
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            raise


def validate_video(ffmpeg, path, size, count, fps):
    result = subprocess.run([str(ffmpeg), '-v', 'error', '-xerror', '-i', str(path), '-f', 'null', '-'],
                            capture_output=True, text=True)
    if result.returncode:
        raise MotionError(result.stderr[-4000:])
    cap = cv2.VideoCapture(str(path))
    actual = (round(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), round(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
              round(cap.get(cv2.CAP_PROP_FRAME_COUNT)), cap.get(cv2.CAP_PROP_FPS))
    cap.release()
    if actual[:3] != (size[0], size[1], count) or abs(actual[3]-fps) > .01:
        raise MotionError(f'Encoded timeline differs from the request: {actual}')
    return dict(decode='passed', frames=count, fps=fps, size=list(size), duration=count/fps)
