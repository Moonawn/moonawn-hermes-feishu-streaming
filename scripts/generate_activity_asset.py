"""Offline authoring recipe for the original GIF (Pillow is build-only)."""
import argparse
import hashlib
import math
from pathlib import Path
from PIL import Image, ImageDraw

def generate(target):
    frames=[]
    for n in range(30):
        frame=Image.new('P',(80,80),0)
        colors=[(0,0,0)]+[(round(148-90*i/15),round(180-66*i/15),round(224-32*i/15)) for i in range(16)]
        frame.putpalette([c for rgb in colors for c in rgb]+[0,0,0]*(256-len(colors)))
        draw=ImageDraw.Draw(frame)
        for i,x in enumerate((17,40,63)):
            pulse=(.5+.5*math.cos(2*math.pi*(n/30-i*.18)))**2
            radius=6+2*pulse
            draw.ellipse((x-radius,40-radius,x+radius,40+radius),fill=1+round(15*pulse))
        frames.append(frame)
    frames[0].save(target,save_all=True,append_images=frames[1:],duration=60,
                   loop=0,transparency=0,disposal=2,optimize=False)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('output',type=Path);args=parser.parse_args()
    if args.output.exists():raise FileExistsError(args.output)
    generate(args.output)
    print(hashlib.sha256(args.output.read_bytes()).hexdigest())
