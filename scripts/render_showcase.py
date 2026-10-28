"""Render a six-technique, 28-second camera showcase from portable demo media."""
from pathlib import Path
import argparse
import json
import math
import shutil
import subprocess
import sys
import wave

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.signal import butter, sosfilt

W, H, FPS, COUNT = 1280, 720, 30, 120
CYAN = (100, 227, 236)
NAMES = ['01-barrel-roll', '02-whip-pan', '03-layered-vertigo',
         '04-occlusion-wipe', '05-match-zoom', '06-speed-ramp']
LABELS = [
    ('BARREL ROLL', 'DIGITAL ROLL / REAL AERIAL FOOTAGE'),
    ('WHIP PAN', 'DIRECTIONAL BLUR / DIGITAL TRANSITION'),
    ('LAYERED VERTIGO', '2.5D COMPOSITE / FIXED FOREGROUND'),
    ('OCCLUSION WIPE', 'TRACKED REAL PILLAR / TWO LOCATIONS'),
    ('MATCH ZOOM', 'DAY TO NIGHT / DIGITAL ZOOM CUT'),
    ('SPEED RAMP', 'REAL TRAIN / VARIABLE PLAYBACK SPEED'),
]
cv2.setNumThreads(2)


def ease(x):
    x = float(np.clip(x, 0., 1.))
    return x*x*(3.-2.*x)


class Clip:
    def __init__(self, path):
        self.cap = cv2.VideoCapture(str(path))
        if not self.cap.isOpened():
            raise FileNotFoundError(path)
        self.length = round(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.cursor, self.cached = -1, None

    def frame(self, index):
        index = int(np.clip(index, 0, self.length - 1))
        if index == self.cursor:
            return self.cached
        if index < self.cursor:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, index)
            self.cursor = index - 1
        while self.cursor < index - 1:
            if not self.cap.grab():
                raise RuntimeError('Source ended during frame selection')
            self.cursor += 1
        ok, self.cached = self.cap.read()
        if not ok:
            raise RuntimeError('Source ended during decode')
        self.cursor = index
        return self.cached

    def close(self):
        self.cap.release()


class Camera:
    def __init__(self):
        self.margins = []
        self.maximum_output_pixels_per_source_pixel = 0.

    def frame(self, src, zoom=1.05, cx=.5, cy=.5, angle=0.):
        sh, sw = src.shape[:2]
        theta = math.radians(angle)
        rot = np.array([[math.cos(theta), math.sin(theta)],
                        [-math.sin(theta), math.cos(theta)]])
        step = min(sw/W, sh/H)/zoom
        corners = np.array([[-(W-1)/2, -(H-1)/2], [(W-1)/2, -(H-1)/2],
                            [(W-1)/2, (H-1)/2], [-(W-1)/2, (H-1)/2]])
        offsets = corners @ (rot*step).T
        # Constrain the crop to photographed pixels, including during rotation.
        half = np.max(np.abs(offsets), axis=0)
        if half[0]*2 > sw-2 or half[1]*2 > sh-2:
            raise ValueError(f'Unsafe rotation/zoom combination: {angle}, {zoom}')
        center = np.array([cx*(sw-1), cy*(sh-1)])
        center = np.clip(center, half+1, np.array([sw-2, sh-2])-half)
        matrix = np.column_stack([rot*step, center - rot @ np.array([(W-1)/2, (H-1)/2])*step])
        pts = offsets+center
        margin = float(min(pts[:,0].min(), pts[:,1].min(),
                           sw-1-pts[:,0].max(), sh-1-pts[:,1].max()))
        self.margins.append(margin)
        self.maximum_output_pixels_per_source_pixel = max(
            self.maximum_output_pixels_per_source_pixel, 1/step)
        assert margin >= .99
        return cv2.warpAffine(src, matrix.astype(np.float32), (W,H),
            flags=cv2.INTER_CUBIC | cv2.WARP_INVERSE_MAP,
            borderMode=cv2.BORDER_CONSTANT)


def overlay(im, number, opacity=1., hero=False):
    image = Image.fromarray(cv2.cvtColor(im, cv2.COLOR_BGR2RGB)).convert('RGBA')
    ink = Image.new('RGBA', (W,H))
    d = ImageDraw.Draw(ink)
    for y in range(H-170, H):
        d.line([(0,y),(W,y)], fill=(5,11,18,round(168*(y-(H-170))/170)))
    title, sub = LABELS[number]
    d.rectangle((42,H-123,46,H-52),fill=(*CYAN,255))
    d.text((62,H-127), f'{number+1:02d} / {title}',
           font=ImageFont.load_default(size=34), fill=(249,248,243,255), stroke_width=1)
    d.text((64,H-76),sub,font=ImageFont.load_default(size=13),fill=(207,221,225,255))
    d.text((W-294,36),'CAMERA MOVEMENT DIRECTION',
           font=ImageFont.load_default(size=13),fill=(241,248,247,240))
    if hero:
        d.rounded_rectangle((40,39,397,137),radius=5,fill=(5,11,18,160))
        d.text((57,51),'CAMERA IN MOTION',font=ImageFont.load_default(size=30),
               fill=(*CYAN,255),stroke_width=1)
        d.text((59,101),'6 TECHNIQUES / ORIGINAL TRAVEL FOOTAGE',
               font=ImageFont.load_default(size=12),fill=(249,248,243,255))
    if opacity < 1:
        a=np.array(ink);a[:,:,3]=np.uint8(a[:,:,3]*opacity);ink=Image.fromarray(a)
    image=Image.alpha_composite(image,ink).convert('RGB')
    return cv2.cvtColor(np.array(image),cv2.COLOR_RGB2BGR)


def make_audio(path):
    """Original procedural electronic rhythm and transition sounds, no samples."""
    sr=48000; duration=28.; rng=np.random.default_rng(20261028)
    mix=np.zeros((int(sr*duration),2),np.float64)

    def add(sound,start,gain=1.,pan=0.):
        start=int(round(start*sr));n=min(len(sound),len(mix)-start)
        if n<=0:return
        a=np.asarray(sound[:n])*gain
        if a.ndim==1:
            mix[start:start+n,0]+=a*np.sqrt((1-pan)/2)
            mix[start:start+n,1]+=a*np.sqrt((1+pan)/2)
        else:mix[start:start+n]+=a

    def tone(freq,duration,decay=6.):
        t=np.arange(int(sr*duration))/sr
        return (np.sin(2*np.pi*freq*t)+.22*np.sin(4*np.pi*freq*t))*np.exp(-decay*t)

    def noise(duration,low,high):
        n=rng.standard_normal(int(sr*duration))
        return sosfilt(butter(2,[low,high],fs=sr,btype='bandpass',output='sos'),n)

    for beat in range(56):
        t=np.arange(int(sr*.28))/sr
        phase=2*np.pi*(46*t+(105-46)*(.030)*(1-np.exp(-t/.030)))
        kick=np.sin(phase)*np.exp(-t*20)+.16*rng.standard_normal(len(t))*np.exp(-t*110)
        add(kick,beat*.5,.62)
        if beat%2:
            t=np.arange(int(sr*.15))/sr
            clap=noise(.15,1000,11000)*np.exp(-t*35)
            add(clap,beat*.5,.28)
        freq=[73.416,73.416,58.270,65.406][(beat//8)%4]
        add(tone(freq,.38,10),beat*.5+.07,.21)
        arp=[293.665,349.228,440.,523.251,440.,349.228,261.626,220.][beat%8]
        s=tone(arp,.42,10)
        add(s,beat*.5+.25,.065,(-1 if beat%2 else 1)*.35)
        add(s,beat*.5+.625,.020,(1 if beat%2 else -1)*.65)
    for k in range(112):
        t=np.arange(int(sr*.055))/sr
        add(noise(.055,7000,17000)*np.exp(-t*90),k*.25,
            .11 if k%2 else .065, .32 if k%2 else -.32)
    for start in [1.0,5.65,8.0,14.35,17.7,20.0,24.0]:
        t=np.arange(int(sr*.7))/sr
        sweep=noise(.7,500,12000)*np.sin(np.pi*np.arange(len(t))/(len(t)-1))**2
        add(sweep,max(0,start-.25),.105)
    for t0 in [0,4,8,12,16,20,24]:
        add(tone(36,.8,7),t0,.33)
    # A quiet pad glues the beat together; an envelope leaves room for the kick.
    t=np.arange(len(mix))/sr
    sidechain=.35+.65*(1-np.exp(-(t%.5)*14))
    for f in [146.832,174.614,220.]:
        pad=np.sin(2*np.pi*f*t)*sidechain*.018
        mix[:,0]+=pad;mix[:,1]+=pad
    mix=np.tanh(mix*1.25)
    # Leave headroom for transient overshoot introduced by AAC encoding.
    mix*=.60/max(np.abs(mix).max(),1e-8)
    fade=np.minimum(1,np.arange(len(mix))/(sr*.03))
    fade*=np.minimum(1,(len(mix)-1-np.arange(len(mix)))/(sr*.5))
    mix*=fade[:,None]
    with wave.open(str(path),'w') as f:
        f.setparams((2,2,sr,0,'NONE','not compressed'))
        f.writeframes(np.int16(np.clip(mix,-1,1)*32767).tobytes())


def encode(ff,path,frames):
    cmd=[ff,'-hide_banner','-loglevel','error','-y','-f','rawvideo','-pix_fmt','bgr24',
         '-s',f'{W}x{H}','-r',str(FPS),'-i','pipe:0','-an',
         '-vf','scale=in_range=pc:out_range=tv:out_color_matrix=bt709,format=yuv420p',
         '-c:v','libx264','-preset','fast','-crf','21','-threads','3',
         '-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709',
         '-color_range','tv','-movflags','+faststart',str(path)]
    proc=subprocess.Popen(cmd,stdin=subprocess.PIPE,stderr=subprocess.PIPE)
    n=0
    try:
        for fr in frames:
            proc.stdin.write(np.ascontiguousarray(fr).tobytes());n+=1
    finally:proc.stdin.close()
    err=proc.stderr.read().decode(errors='replace');code=proc.wait()
    if code:raise RuntimeError(err)
    if n!=COUNT:raise RuntimeError((path,n))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--input-dir',type=Path,default=Path('media'))
    parser.add_argument('--output-dir',type=Path,default=Path('examples'))
    parser.add_argument('--ffmpeg',default=shutil.which('ffmpeg'))
    args=parser.parse_args()
    if not args.ffmpeg:parser.error('Set --ffmpeg to an FFmpeg executable with libx264')
    m,o=args.input_dir,args.output_dir;o.mkdir(parents=True,exist_ok=True)
    layered_dir=o/'.layered-cache'
    subprocess.run([sys.executable,str(Path(__file__).with_name('layered_vertigo.py')),
                    '--input-dir',str(m/'vertigo-layers'),'--output-dir',str(layered_dir),
                    '--ffmpeg',str(args.ffmpeg)],check=True,stdout=subprocess.DEVNULL)
    clips={n:Clip(m/(n+'.mp4')) for n in ['gardens','street','bay','airport','jewel-night','train']}
    clips['layered-vertigo']=Clip(layered_dir/'layered_vertigo_4s.mp4')
    day=cv2.imread(str(m/'jewel-day.jpg'));assert day is not None
    cams=[Camera() for _ in NAMES]
    preview={}; report={}
    weights=1-np.exp(-((np.arange(COUNT-1)-52)/17)**2)
    steps=1+((clips['train'].length-1)-(COUNT-1))/weights.sum()*weights
    train_indices=np.r_[0,np.cumsum(steps)].round().astype(int)
    assert np.diff(train_indices).min()>=1

    def effect(k,i):
        t=i/FPS;c=cams[k]
        if k==0:
            z=1.09+1.07*(ease(t/.45)-ease((t-3.15)/.8))
            angle=360*ease((t-.55)/2.55)
            src=clips['gardens'].frame(i)
            fr=c.frame(src,z,angle=angle)
            if .6<t<3.05:
                a=c.frame(src,z,angle=angle-2.5)
                b=c.frame(src,z,angle=angle+2.5)
                fr=cv2.addWeighted(fr,.6,cv2.addWeighted(a,.5,b,.5,0),.4,0)
        elif k==1:
            a=clips['street'].frame(min(i,85))
            b=clips['bay'].frame(max(0,i-51))
            a=cv2.resize(a,(W,H));b=cv2.resize(b,(W,H))
            if i<51:fr=a
            elif i>67:fr=b
            else:
                u=ease((i-51)/16);x=round(u*W)
                fr=np.concatenate([a,b],axis=1)[:,x:x+W].copy()
                blur=1+2*round(35*math.sin(math.pi*u))
                fr=cv2.blur(fr,(blur,1))
        elif k==2:
            fr=cv2.resize(clips['layered-vertigo'].frame(i),(W,H))
        elif k==3:
            if i<87:
                a=cv2.resize(clips['airport'].frame(153+i),(W,H))
                fr=a
                if i>=54:
                    mask=cv2.imread(str(m/'pillar-masks'/f'{i-54:04d}.png'),0)
                    if mask is None:raise FileNotFoundError('Missing pillar mask')
                    b=cv2.resize(clips['jewel-night'].frame(i-54),(W,H))
                    alpha=mask.astype(np.float32)[:,:,None]/255
                    fr=np.uint8(a*(1-alpha)+b*alpha+.5)
            else:fr=cv2.resize(clips['jewel-night'].frame(i-54),(W,H))
        elif k==4:
            p=min(1,i/59)
            a=c.frame(day,1.08+4.50*p**4,cx=.5-.20*p*p,cy=.5-.31*p*p)
            if i<56:fr=a
            else:
                q=np.clip((i-60)/59,0,1)
                b=c.frame(clips['jewel-night'].frame(max(0,i-56)),
                          1.1+4.5*(1-q)**4,cx=.5+.22*(1-q)**2,cy=.5-.10*(1-q)**2)
                if i<64:
                    u=ease((i-56)/8)
                    fr=cv2.addWeighted(a,1-u,b,u,0)
                else:fr=b
            peak=math.exp(-((i-59.5)/7.0)**2)
            if peak>.05:fr=cv2.GaussianBlur(fr,(0,0),.1+3.0*peak)
        else:
            fr=cv2.resize(clips['train'].frame(train_indices[i]),(W,H))
        if i in [0,30,60,90,119]:preview[(k,i)]=fr.copy()
        return overlay(fr,k,hero=(k==0 and i<30))

    try:
        for k,name in enumerate(NAMES):
            print('RENDER',name,flush=True)
            encode(args.ffmpeg,o/(name+'.mp4'),(effect(k,i) for i in range(COUNT)))
            report[name]=dict(frames=COUNT,fps=FPS,duration=4,
                camera_samples=len(cams[k].margins),
                minimum_source_edge_margin=min(cams[k].margins) if cams[k].margins else None,
                maximum_output_pixels_per_source_pixel=cams[k].maximum_output_pixels_per_source_pixel)
    finally:
        for clip in clips.values():clip.close()

    def outro():
        picks=[(0,60),(1,60),(2,60),(3,90),(4,90),(5,60)]
        for i in range(COUNT):
            if i<36:
                k,j=picks[i//6];fr=preview[(k,j)]
            else:
                bg=preview[(0,119)].astype(np.float32)*.20
                bg+=np.array([14,9,4],np.float32)
                image=Image.fromarray(cv2.cvtColor(np.uint8(bg),cv2.COLOR_BGR2RGB))
                d=ImageDraw.Draw(image)
                d.text((67,152),'CAMERA',font=ImageFont.load_default(size=90),fill='#f8f7f1',stroke_width=2)
                d.text((67,247),'IN MOTION',font=ImageFont.load_default(size=90),fill='#64e3ec',stroke_width=2)
                d.text((73,376),'6 TECHNIQUES. ONE SKILL.',font=ImageFont.load_default(size=26),fill='#e7f0f1')
                d.text((74,425),'camera-movement-direction',font=ImageFont.load_default(size=21),fill='#a9bdc6')
                for k in range(3):
                    thumb=cv2.resize(preview[(k*2,60)],(288,162))
                    image.paste(Image.fromarray(cv2.cvtColor(thumb,cv2.COLOR_BGR2RGB)),(916,80+k*179))
                fr=cv2.cvtColor(np.array(image),cv2.COLOR_RGB2BGR)
            yield fr
    encode(args.ffmpeg,o/'07-outro.mp4',outro())
    concat=o/'concat.txt'
    concat.write_text('\n'.join("file '"+(o/(n+'.mp4')).resolve().as_posix().replace("'","'\\''")+"'" for n in NAMES+['07-outro'])+'\n')
    music=o/'original-score.wav';make_audio(music)
    cmd=[args.ffmpeg,'-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(concat),
         '-i',str(music),'-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac','-b:a','192k',
         '-t','28','-map_metadata','-1','-movflags','+faststart',str(o/'showreel.mp4')]
    subprocess.run(cmd,check=True)
    concat.unlink()
    report['barrel_roll_degrees']=360
    report['train_source_frames']=train_indices.tolist()
    report['audio']='Original procedural rhythm and synthesized transition sounds; deterministic seed 20261028.'
    (o/'render-report.json').write_text(json.dumps(report,indent=2))
    print('DONE',o/'showreel.mp4',flush=True)


if __name__=='__main__':main()
