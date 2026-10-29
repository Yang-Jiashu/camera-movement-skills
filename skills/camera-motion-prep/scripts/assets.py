"""Run standalone CV asset preparation from a repository or installed runtime."""
from pathlib import Path
import argparse
import subprocess
import sys


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['masks','layers','match'])
    args=p.parse_args(sys.argv[1:2])
    rest=sys.argv[2:]
    filename={'masks':'motion_masks.py','layers':'motion_layers.py','match':'motion_match.py'}[args.command]
    root=Path(__file__).resolve().parents[2]
    runtime=next((p for p in [root/'.camera-motion-runtime'/filename,root.parent/'scripts'/filename] if p.is_file()),None)
    if runtime is None:raise SystemExit('Shared CV preparation runtime is missing. Upgrade the full skill library.')
    return subprocess.call([sys.executable,str(runtime),*rest])


if __name__=='__main__':raise SystemExit(main())
