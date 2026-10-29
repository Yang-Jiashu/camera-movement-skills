"""Prepare CV assets and render this skill through the shared runtime."""
from pathlib import Path
import subprocess
import sys


def main():
    here = Path(__file__).resolve()
    skills_root = here.parents[2]
    candidates = [skills_root/'.camera-motion-runtime/camera_pipeline.py',
                  skills_root.parent/'scripts/camera_pipeline.py']
    runtime = next((p for p in candidates if p.is_file()), None)
    if runtime is None:
        raise SystemExit('CV pipeline is missing. Upgrade the full camera-movement-skills installation.')
    return subprocess.call([sys.executable, str(runtime), 'parallax', *sys.argv[1:]])


if __name__ == '__main__':
    raise SystemExit(main())
