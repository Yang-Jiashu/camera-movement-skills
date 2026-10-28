"""Run this skill through the bundled or installed shared camera runtime."""
from pathlib import Path
import subprocess
import sys


def main():
    here = Path(__file__).resolve()
    skills_root = here.parents[2]
    candidates = [skills_root/'.camera-motion-runtime/apply_camera_motion.py',
                  skills_root.parent/'scripts/apply_camera_motion.py']
    runtime = next((p for p in candidates if p.is_file()), None)
    if runtime is None:
        raise SystemExit('Shared camera runtime is missing. Install from the full camera-movement-skills repository with scripts/install_skills.py.')
    return subprocess.call([sys.executable, str(runtime), 'roll', *sys.argv[1:]])


if __name__ == '__main__':
    raise SystemExit(main())
