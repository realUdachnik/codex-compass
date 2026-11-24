#!/usr/bin/env python3
"""Install this standalone Codex skill using only Python's standard library."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
import uuid


MARKER = '.codex-compass-install.json'
OWNER = 'codex-compass-standalone-bundle'


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _files(root: Path, *, installed: bool = False) -> dict[str, str]:
    result = {}
    for directory, children, names in os.walk(root, followlinks=False):
        base = Path(directory)
        for child in list(children):
            candidate = base / child
            if candidate.is_symlink():
                raise ValueError('Символические ссылки не допускаются в установленном комплекте')
            if installed and child == '__pycache__':
                children.remove(child)
        for name in names:
            path = base / name
            relative = path.relative_to(root).as_posix()
            if installed and relative == MARKER:
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError('В комплекте разрешены только обычные файлы: ' + relative)
            result[relative] = _hash(path)
    return result


def _manifest(bundle: Path) -> tuple[dict, Path]:
    path = bundle / 'bundle-manifest.json'
    if path.is_symlink() or path.stat().st_size > 2_000_000:
        raise ValueError('Некорректное описание комплекта')
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(manifest, dict) or manifest.get('schema_version') != 1 or
            manifest.get('name') != 'codex-compass' or
            not isinstance(manifest.get('version'), str) or not manifest['version']):
        raise ValueError('Этот установщик принимает только комплект Codex Compass')
    hashes = manifest.get('files')
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError('В описании комплекта нет файлов')
    for name, digest in hashes.items():
        rel = PurePosixPath(name)
        if (not isinstance(name, str) or rel.is_absolute() or '..' in rel.parts or
                '\\' in name or not rel.parts or not isinstance(digest, str) or len(digest) != 64):
            raise ValueError('Недопустимый путь или отпечаток файла в комплекте')
    if any(name not in hashes for name in ('SKILL.md', 'scripts/compass.py',
                                         'runtime/codex_compass/cli.py', 'runtime/codex_compass/__init__.py')):
        raise ValueError('Комплект неполон')
    payload = bundle / 'payload' / 'codex-compass'
    if (bundle / 'payload').is_symlink() or payload.is_symlink() or not payload.is_dir():
        raise ValueError('Некорректная папка файлов комплекта')
    if _files(payload) != hashes:
        raise ValueError('Файлы комплекта отличаются от описания; установка не выполнялась')
    return manifest, payload


def default_target() -> Path:
    home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser()
    return home / 'skills' / 'codex-compass'


@contextmanager
def _lock(target: Path):
    # The runtime itself targets POSIX. A persistent flock inode also prevents
    # two installers from replacing a directory between each other's checks.
    import fcntl
    lock_path = target.parent / ('.' + target.name + '.install.lock')
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('Другая установка в эту папку уже выполняется') from error
        yield
    finally:
        os.close(fd)


def install_bundle(bundle: Path, target: Path | None = None) -> dict:
    if sys.version_info < (3, 11):
        raise ValueError('Нужен Python 3.11 или новее')
    if os.name != 'posix':
        raise ValueError('Нужна Linux/macOS или Linux-среда WSL в Windows')
    bundle = Path(bundle).expanduser().resolve(strict=True)
    manifest, payload = _manifest(bundle)
    target = Path(target) if target is not None else default_target()
    target = target.expanduser().absolute()
    if target.is_symlink():
        raise ValueError('Папка назначения — ссылка; существующая установка не изменена')
    target.parent.mkdir(parents=True, exist_ok=True)
    target = target.parent.resolve() / target.name
    with _lock(target):
        if target.is_symlink():
            raise ValueError('Папка назначения — ссылка; существующая установка не изменена')
        previous = None
        if target.exists():
            marker = target / MARKER
            if not target.is_dir() or marker.is_symlink() or not marker.is_file():
                raise ValueError('Эта папка не создана нашим установщиком; её файлы не изменены')
            previous = json.loads(marker.read_text(encoding='utf-8'))
            if (not isinstance(previous, dict) or previous.get('owner') != OWNER or
                    previous.get('schema_version') != 1):
                raise ValueError('У папки другая метка владельца; её файлы не изменены')
            if _files(target, installed=True) != previous.get('files'):
                raise ValueError('В установленной папке есть собственные изменения; они не перезаписаны')
            if previous.get('files') == manifest['files'] and previous.get('version') == manifest.get('version'):
                return {'status': 'unchanged', 'target': str(target), 'version': manifest['version'],
                        'launcher': str(target / 'scripts/compass.py')}
        stage = Path(tempfile.mkdtemp(prefix='.' + target.name + '.new-', dir=target.parent))
        backup = None
        try:
            shutil.copytree(payload, stage, dirs_exist_ok=True)
            if _files(stage) != manifest['files']:
                raise ValueError('Файлы изменились во время копирования; прежняя установка сохранена')
            marker = {'schema_version': 1, 'owner': OWNER, 'version': manifest['version'],
                      'source_version': manifest.get('source_version'), 'files': manifest['files']}
            (stage / MARKER).write_text(json.dumps(marker, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
            if target.exists():
                backup = target.parent / ('.' + target.name + '.previous-' + uuid.uuid4().hex)
                target.rename(backup)
            try:
                stage.rename(target)
            except BaseException:
                if backup is not None:
                    backup.rename(target)
                    backup = None
                raise
            if backup is not None:
                shutil.rmtree(backup)
                backup = None
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return {'status': 'updated' if previous is not None else 'installed', 'target': str(target),
                'version': manifest['version'], 'launcher': str(target / 'scripts/compass.py')}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, default=Path(__file__).resolve().parent,
                        help='Распакованная папка с bundle-manifest.json; по умолчанию рядом с install.py')
    parser.add_argument('--target', type=Path, help='Точная папка навыка; по умолчанию CODEX_HOME/skills/codex-compass')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(install_bundle(args.bundle, args.target), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'error', 'error': str(error)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
