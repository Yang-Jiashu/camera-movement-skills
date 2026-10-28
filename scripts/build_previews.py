"""Build the README contact sheet and an eight-second looping GIF teaser."""
from pathlib import Path
import argparse
import shutil
import subprocess
import cv2
from PIL import Image, ImageDraw, ImageFont


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--examples-dir',type=Path,default=Path('examples'))
    parser.add_argument('--docs-dir',type=Path,default=Path('docs'))
    parser.add_argument('--ffmpeg',default=shutil.which('ffmpeg'))
    args=parser.parse_args()
    if not args.ffmpeg:parser.error('Supply --ffmpeg')
    d=args.docs_dir;d.mkdir(parents=True,exist_ok=True)
    names=['01-barrel-roll','02-whip-pan','03-layered-vertigo',
           '04-occlusion-wipe','05-match-zoom','06-speed-ramp']
    canvas=Image.new('RGB',(1920,880),'#0a1018');draw=ImageDraw.Draw(canvas)
    draw.text((36,24),'CAMERA IN MOTION',font=ImageFont.load_default(size=40),fill='#64e3ec',stroke_width=1)
    draw.text((1330,42),'6 TECHNIQUES / ONE SKILL',font=ImageFont.load_default(size=20),fill='#dfe9eb')
    for k,(name,index) in enumerate(zip(names,[44,83,90,69,90,52])):
        cap=cv2.VideoCapture(str(args.examples_dir/(name+'.mp4')))
        cap.set(cv2.CAP_PROP_POS_FRAMES,index);ok,fr=cap.read();cap.release()
        if not ok:raise RuntimeError(f'Cannot sample {name}')
        x=(k%3)*640;y=100+(k//3)*390
        canvas.paste(Image.fromarray(cv2.cvtColor(fr,cv2.COLOR_BGR2RGB)).resize((640,360)),(x,y))
        draw.text((x+18,y+363),name.upper(),font=ImageFont.load_default(size=16),fill='#b0c4ca')
    canvas.save(d/'contact-sheet.jpg',quality=94)
    filters=(
        '[0:v]split=4[a][b][c][d];'
        '[a]trim=start=0:end=4,setpts=0.5*(PTS-STARTPTS)[aa];'
        '[b]trim=start=8:end=12,setpts=0.5*(PTS-STARTPTS)[bb];'
        '[c]trim=start=12:end=16,setpts=0.5*(PTS-STARTPTS)[cc];'
        '[d]trim=start=16:end=20,setpts=0.5*(PTS-STARTPTS)[dd];'
        '[aa][bb][cc][dd]concat=n=4:v=1:a=0,fps=10,scale=480:270:flags=lanczos,split[x][y];'
        '[x]palettegen=max_colors=64:stats_mode=full[p];'
        '[y][p]paletteuse=dither=bayer:bayer_scale=5')
    subprocess.run([args.ffmpeg,'-hide_banner','-loglevel','error','-y',
                    '-i',str(args.examples_dir/'showreel.mp4'),'-filter_complex',filters,
                    '-an','-loop','0',str(d/'preview.gif')],check=True)
    print('Built',d/'preview.gif',d/'contact-sheet.jpg')


if __name__=='__main__':main()
