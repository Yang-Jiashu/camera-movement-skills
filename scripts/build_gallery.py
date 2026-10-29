"""Build the README identity banner and six GIFs from the actual example videos.

Run from any directory. Requires the repository's Pillow dependency and FFmpeg.
No footage is generated, interpolated, or retimed: every GIF shows the complete
four-second example at eight sampled frames per second.
"""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps


REPO = Path(__file__).resolve().parents[1]
CARDS = (
    ("01-barrel-roll", "BARREL ROLL", "360 DEGREE ROTATION"),
    ("02-whip-pan", "WHIP PAN", "DIRECTIONAL TRANSITION"),
    ("03-layered-vertigo", "LAYERED VERTIGO", "2.5D COMPOSITE"),
    ("04-occlusion-wipe", "OCCLUSION WIPE", "TRACKED FOREGROUND"),
    ("05-match-zoom", "MATCH ZOOM", "DAY TO NIGHT"),
    ("06-speed-ramp", "SPEED RAMP", "VARIABLE PLAYBACK"),
)
INK = "#0b131a"
WHITE = "#f0f4f3"
MINT = "#6ce7d0"
MUTED = "#a3b7bc"


def font(size):
    return ImageFont.load_default(size=size)


def build_hero(destination):
    """Code-drawn typography and a viewfinder motif, not a footage simulation."""
    canvas = Image.new("RGB", (1440, 300), INK)
    draw = ImageDraw.Draw(canvas)
    for x in range(1020, 1440, 40):
        draw.line((x, 0, x, 300), fill="#14222b", width=1)
    for y in range(0, 300, 40):
        draw.line((1000, y, 1440, y), fill="#14222b", width=1)
    draw.rectangle((48, 42, 53, 199), fill=MINT)
    draw.text((76, 40), "CAMERA", font=font(66), fill=WHITE)
    draw.text((76, 111), "MOVEMENT SKILLS", font=font(66), fill=WHITE)
    draw.text((79, 231), "REAL FOOTAGE.  PURPOSEFUL MOTION.", font=font(19), fill=MUTED)
    # A frame with a motion path; deliberately an abstract brand graphic.
    x0, y0, x1, y1 = 1080, 65, 1350, 213
    for coords in (
        (x0, y0 + 23, x0, y0, x0 + 23, y0),
        (x1 - 23, y0, x1, y0, x1, y0 + 23),
        (x0, y1 - 23, x0, y1, x0 + 23, y1),
        (x1 - 23, y1, x1, y1, x1, y1 - 23),
    ):
        draw.line(coords, fill=MINT, width=3)
    points = [(1102, 172), (1131, 172), (1163, 156), (1195, 124), (1232, 107), (1299, 107)]
    draw.line(points, fill=MINT, width=4, joint="curve")
    draw.polygon(((1299, 107), (1282, 98), (1282, 116)), fill=MINT)
    draw.ellipse((1095, 165, 1109, 179), fill="#ff8a70")
    draw.text((1084, 242), "DESIGN / PREP / EDIT", font=font(16), fill=MUTED)
    canvas.save(destination / "hero.png", optimize=True)


def sample_video(ffmpeg, source, width, height, fps):
    decode = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(source),
         "-vf", f"fps={fps},scale={width}:{height}:flags=lanczos", "-an",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        check=True, stdout=subprocess.PIPE,
    ).stdout
    frame_size = width * height * 3
    if not decode or len(decode) % frame_size:
        raise RuntimeError(f"Incomplete decoded frames: {source.name}")
    return [Image.frombytes("RGB", (width, height), decode[i:i + frame_size])
            for i in range(0, len(decode), frame_size)]


def write_gif(ffmpeg, frames, width, height, fps, destination):
    # One shared palette and ordered dither keep the small previews stable.
    filters = ("split[a][b];[a]palettegen=max_colors=96:stats_mode=full[p];"
               "[b][p]paletteuse=dither=bayer:bayer_scale=4")
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
         "-r", str(fps), "-i", "pipe:0", "-filter_complex", filters,
         "-loop", "0", "-final_delay",
         str(round(len(frames) * 100 / fps) - round((len(frames) - 1) * 100 / fps)),
         str(destination)],
        input=b"".join(frame.tobytes() for frame in frames), check=True,
    )
    with Image.open(destination) as check:
        count = check.n_frames
        duration = 0
        for index in range(count):
            check.seek(index)
            duration += check.info.get("duration", 0)
        if count != len(frames) or check.info.get("loop") != 0:
            raise RuntimeError(f"GIF verification failed: {destination.name}")
    return {
        "gif": destination.name,
        "width": width, "height": height, "frames": count,
        "duration_ms": duration, "bytes": destination.stat().st_size,
        "source_timing": "unchanged; sampled at 8 fps" if fps == 8 else f"unchanged; sampled at {fps} fps",
    }


def make_card(ffmpeg, source, destination, number, title, detail, width, fps):
    height = round(width * 9 / 16)
    height += height % 2
    header = 57
    total_height = header + height + 6
    frames = []
    for image in sample_video(ffmpeg, source, width, height, fps):
        card = Image.new("RGB", (width, total_height), INK)
        draw = ImageDraw.Draw(card)
        draw.rectangle((0, 0, 3, header - 1), fill=MINT)
        draw.text((15, 10), title, font=font(17), fill=WHITE)
        draw.text((15, 34), detail, font=font(9), fill=MUTED)
        draw.text((width - 28, 12), f"{number:02}", font=font(11), fill=MINT)
        card.paste(image, (0, header))
        frames.append(card)
    return dict(source=source.name, **write_gif(ffmpeg, frames, width, total_height, fps, destination))


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_pipeline_proof(ffmpeg, source, layers, video, destination, fps):
    """Visualize actual prepared assets; hide uncertified background explicitly."""
    if (layers / "layers").is_dir():
        layers = layers / "layers"
    manifest = json.loads((layers / "manifest.json").read_text())
    if manifest["status"] != "accepted" or manifest["input_sha256"] != sha256(source):
        raise RuntimeError("Pipeline proof requires accepted assets from this exact source")
    width, height, header, footer, gap = 280, 210, 50, 31, 8
    panel_width = width + gap
    total_width = 4 * width + 3 * gap
    total_height = header + height + footer
    base = Image.new("RGB", (total_width, total_height), INK)
    original = ImageOps.exif_transpose(Image.open(source)).convert("RGB")
    original = ImageOps.fit(original, (width, height), method=Image.Resampling.LANCZOS)
    source_draw = ImageDraw.Draw(original)
    roi = manifest["segmentation"].get("roi")
    if roi:
        x, y, w, h = roi
        source_draw.rectangle((x * width, y * height, (x + w) * width, (y + h) * height), outline=MINT, width=2)
    for field, color in (("foreground_points", MINT), ("background_points", "#ff8a70")):
        for x, y in manifest["segmentation"].get(field, []):
            px, py = x * width, y * height
            source_draw.ellipse((px - 3, py - 3, px + 3, py + 3), fill=color)
    alpha = Image.open(layers / "alpha.png").convert("L").resize((width, height), Image.Resampling.LANCZOS)
    background = Image.open(layers / "background.png").convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
    validity = Image.open(layers / "background-validity.png").convert("L").resize((width, height), Image.Resampling.NEAREST)
    repair = Image.open(layers / "repair-mask.png").convert("L").resize((width, height), Image.Resampling.NEAREST)
    # A filled pixel is not recovered truth. The unknown core is visibly masked,
    # while cyan marks the small repair band approved for this exact motion.
    hatch = Image.new("RGB", (width, height), "#35262c")
    hatch_draw = ImageDraw.Draw(hatch)
    for x in range(-height, width, 12):
        hatch_draw.line((x, height, x + height, 0), fill="#e49781", width=2)
    repaired_tint = Image.blend(background, Image.new("RGB", (width, height), "#4de1d0"), .72)
    background = Image.composite(repaired_tint, background, ImageChops.multiply(repair, validity))
    background = Image.composite(background, hatch, validity)
    for i, picture in enumerate((original, alpha.convert("RGB"), background)):
        base.paste(picture, (i * panel_width, header))
    draw = ImageDraw.Draw(base)
    labels = ("01 / INPUT + ROI", "02 / GRABCUT MASK", "03 / LOCAL REPAIR", "04 / 2.5D RESULT")
    notes = ("USER ROI + POINT PROMPTS", "WHITE = FOREGROUND", "CYAN: FILL  /  HATCH: UNKNOWN", "ACTUAL PIPELINE OUTPUT")
    for i, (label, note) in enumerate(zip(labels, notes)):
        x = i * panel_width
        draw.rectangle((x, 6, x + 3, 34), fill=MINT)
        draw.text((x + 12, 12), label, font=font(16), fill=WHITE)
        draw.text((x + 4, header + height + 10), note, font=font(10), fill=MUTED)
    frames = []
    output_height = round(width * 9 / 16)
    for picture in sample_video(ffmpeg, video, width, output_height, fps):
        frame = base.copy()
        frame.paste(picture, (3 * panel_width, header + (height - output_height) // 2))
        frames.append(frame)
    result = write_gif(ffmpeg, frames, total_width, total_height, fps, destination)
    result.update({
        "source": source.name, "source_sha256": sha256(source),
        "video": video.name, "video_sha256": sha256(video),
        "asset_manifest_sha256": sha256(layers / "manifest.json"),
        "asset_hashes": {name: sha256(layers / name) for name in (
            "alpha.png", "background.png", "repair-mask.png", "background-validity.png")},
        "initialization": "User ROI and foreground/background points; actual GrabCut output",
        "motion_factor": manifest["motion_factor"],
        "background_legend": "cyan = local repair approved for this motion; hatch = uncertified hidden core, not recovered scene truth",
    })
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--examples-dir", type=Path, default=REPO / "examples")
    parser.add_argument("--output-dir", type=Path, default=REPO / "docs" / "gallery")
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg"))
    parser.add_argument("--width", type=int, default=360)
    parser.add_argument("--fps", type=int, default=8)
    parser.add_argument("--pipeline-assets-dir", type=Path,
                        help="Optionally include a proof strip from an actual accepted parallax run's .assets directory.")
    parser.add_argument("--pipeline-source", type=Path, default=REPO / "media" / "flag.jpg")
    parser.add_argument("--pipeline-video", type=Path, default=REPO / "examples" / "pipeline" / "auto-parallax.mp4")
    args = parser.parse_args()
    if not args.ffmpeg:
        parser.error("Supply --ffmpeg /path/to/ffmpeg or put FFmpeg on PATH")
    if args.width < 240 or args.width > 720 or args.fps < 1 or args.fps > 30:
        parser.error("Use width 240–720 and fps 1–30")
    for name, _, _ in CARDS:
        if not (args.examples_dir / f"{name}.mp4").is_file():
            parser.error(f"Missing source: {name}.mp4")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    # Stage all outputs so a failed conversion cannot replace a finished card.
    with tempfile.TemporaryDirectory(prefix="camera-gallery-") as temporary:
        stage = Path(temporary)
        build_hero(stage)
        cards = []
        for number, (name, title, detail) in enumerate(CARDS, 1):
            cards.append(make_card(args.ffmpeg, args.examples_dir / f"{name}.mp4",
                                   stage / f"{name}.gif", number, title, detail,
                                   args.width, args.fps))
            print(f"Built {name}: {cards[-1]['bytes'] / 1_000_000:.2f} MB")
        manifest = {
            "description": "Complete existing demo clips, sampled without retiming; labels added outside footage.",
            "cards": cards,
        }
        if args.pipeline_assets_dir:
            manifest["pipeline_proof"] = make_pipeline_proof(
                args.ffmpeg, args.pipeline_source, args.pipeline_assets_dir, args.pipeline_video,
                stage / "pipeline-proof.gif", args.fps)
            print("Built actual pipeline proof:", manifest["pipeline_proof"]["bytes"], "bytes")
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for output in stage.iterdir():
            shutil.copy2(output, args.output_dir / output.name)
    print("Gallery ready:", args.output_dir)


if __name__ == "__main__":
    main()
