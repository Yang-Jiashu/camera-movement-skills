"""Install selected skills plus their shared local video runtime without pip changes."""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
import shutil
import tempfile
import uuid

REPO = Path(__file__).resolve().parent.parent
RUNTIME_FILES = ['motion_core.py', 'apply_camera_motion.py', 'prepare_motion.py']


def fingerprint(root):
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if not path.is_file() or '__pycache__' in path.parts or path.suffix == '.pyc':
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default_root = Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))) / 'skills'
    parser.add_argument('--dest', type=Path, default=default_root,
                        help='Skill installation directory; defaults to CODEX_HOME/skills')
    parser.add_argument('--skill', action='append', dest='names',
                        help='Install this skill; repeat to select several. Default: all bundled skills.')
    parser.add_argument('--upgrade', action='store_true',
                        help='Back up and replace existing changed copies of the selected skills/runtime.')
    parser.add_argument('--dry-run', action='store_true', help='Describe changes without writing files.')
    args = parser.parse_args()
    source = REPO/'skills'
    available = {p.name:p for p in source.iterdir() if (p/'SKILL.md').is_file()}
    names = sorted(set(args.names or available))
    unknown = set(names)-set(available)
    if unknown:
        parser.error('Unknown skill(s): '+', '.join(sorted(unknown)))
    for filename in RUNTIME_FILES:
        if not (REPO/'scripts'/filename).is_file():
            parser.error('Shared runtime missing from this checkout: '+filename)
    dest = args.dest.expanduser().resolve()
    if dest == source.resolve():
        parser.error('Choose an installation directory outside the repository skills directory.')
    changes = []
    for name in names:
        target = dest/name
        if target.exists() and fingerprint(target) == fingerprint(available[name]):
            changes.append((name,'unchanged'))
        elif target.exists():
            changes.append((name,'upgrade'))
        else:
            changes.append((name,'install'))
    runtime = dest/'.camera-motion-runtime'
    runtime_changed = any(not (runtime/f).is_file() or
                          (runtime/f).read_bytes() != (REPO/'scripts'/f).read_bytes()
                          for f in RUNTIME_FILES)
    conflicts = [name for name,action in changes if action == 'upgrade']
    if runtime.exists() and runtime_changed:
        conflicts.append('.camera-motion-runtime')
    if conflicts and not args.upgrade:
        parser.error('Existing copies differ: '+', '.join(conflicts)+
                     '. Use --upgrade to back them up before replacement.')
    plan = dict(destination=str(dest), skills=dict(changes),
                shared_runtime='update' if runtime_changed else 'unchanged',
                dependencies='Install Python requirements separately; this command does not run pip.')
    print(json.dumps(plan,ensure_ascii=False,indent=2))
    if args.dry_run:
        return
    dest.mkdir(parents=True,exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
    backup = dest.parent/'camera-motion-backups'/stamp
    changed = []
    with tempfile.TemporaryDirectory(prefix='.camera-motion-install-',dir=dest.parent) as temp:
        staging = Path(temp)
        for name,action in changes:
            if action == 'unchanged':
                continue
            shutil.copytree(available[name], staging/name,
                            ignore=shutil.ignore_patterns('__pycache__','*.pyc','.DS_Store'))
            changed.append((name,staging/name))
        if runtime_changed:
            rt = staging/'.camera-motion-runtime'
            rt.mkdir()
            for filename in RUNTIME_FILES:
                shutil.copyfile(REPO/'scripts'/filename,rt/filename)
            (rt/'manifest.json').write_text(json.dumps({
                'runtime_api':1,'source':'camera-movement-skills',
                'files':{f:hashlib.sha256((rt/f).read_bytes()).hexdigest() for f in RUNTIME_FILES}
            },indent=2))
            changed.append(('.camera-motion-runtime',rt))
        completed = []
        try:
            for name,new in changed:
                target = dest/name
                old = None
                if target.exists():
                    backup.mkdir(parents=True,exist_ok=True)
                    old = backup/name
                    target.rename(old)
                try:
                    new.rename(target)
                except Exception:
                    if old is not None:
                        old.rename(target)
                    raise
                completed.append((target,old))
        except Exception:
            # Recover the pre-install versions if a later installation fails.
            for target,old in reversed(completed):
                shutil.rmtree(target)
                if old is not None:
                    old.rename(target)
            raise
    print('Installed '+str(len(names))+' skill(s); shared runtime ready.')
    if backup.exists():
        print('Previous copies saved in '+str(backup))


if __name__ == '__main__':
    main()
