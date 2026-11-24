"""Content-based evidence. Ignored generated directories are not claims of coverage."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

IGNORED = {'.git', '__pycache__', '.venv', 'node_modules', '.compass', '.pytest_cache', '.mypy_cache', '.ruff_cache'}


def _field(digest, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, 'big'))
    digest.update(value)


def _entry(digest, path: Path, label: str, *, missing: bool = False) -> None:
    _field(digest, os.fsencode(label))
    try:
        before = path.lstat()
    except FileNotFoundError:
        if not missing:
            raise
        _field(digest, b'missing')
        return
    _field(digest, str(stat.S_IFMT(before.st_mode)).encode())
    _field(digest, str(stat.S_IMODE(before.st_mode)).encode())
    if stat.S_ISLNK(before.st_mode):
        _field(digest, os.fsencode(os.readlink(path)))
    elif stat.S_ISREG(before.st_mode):
        _field(digest, str(before.st_size).encode())
        with path.open('rb') as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        after = path.lstat()
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode):
            raise ValueError(f'File changed while hashing: {label}')
    elif not stat.S_ISDIR(before.st_mode):
        raise ValueError(f'Unsupported special file: {label}')
    _field(digest, b'end')


def _git_tracked(root: Path) -> tuple[set[str], set[str]]:
    tracked: set[str] = set()
    try:
        result = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--cached'],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=5, check=False)
        if result.returncode == 0:
            tracked = {os.fsdecode(x) for x in result.stdout.split(b'\0') if x}
    except FileNotFoundError:
        pass
    # Include tracked source even under a conventional generated directory.
    prefixes = {str(Path(name).parent) for name in tracked}
    for name in list(prefixes):
        prefixes.update(str(x) for x in Path(name).parents)
    return tracked, prefixes


def tree_digest(workspace: Path, exclude: tuple[Path, ...] = ()) -> str:
    root = Path(workspace).resolve(strict=True)
    excluded = tuple(Path(x).absolute() for x in exclude)
    tracked, prefixes = _git_tracked(root)
    digest = hashlib.sha256()
    def visit(path: Path, rel: str) -> None:
        if any(path == x or x in path.parents for x in excluded):
            return
        if path.name == '.git' and rel:
            return
        if rel and path.name in IGNORED and rel not in tracked and rel not in prefixes:
            return
        _entry(digest, path, rel or '.')
        if path.is_dir() and not path.is_symlink():
            for child in sorted(path.iterdir(), key=lambda p: os.fsencode(p.name)):
                visit(child, f'{rel}/{child.name}' if rel else child.name)
    visit(root, '')
    return digest.hexdigest()


def protected_digest(paths: tuple[Path, ...]) -> str:
    """Protect explicit paths; omit incidental caches under protected directories.

    Explicit files are always hashed, including files inside generated folders.
    Git-tracked files override the cache exclusion. The protected root itself is
    never excluded merely because its name resembles a generated directory.
    """
    digest = hashlib.sha256()
    for root in sorted((Path(x).absolute() for x in paths), key=str):
        tracked, prefixes = _git_tracked(root) if root.is_dir() and not root.is_symlink() else (set(), set())
        def visit(path: Path, rel: str, ignored_parent: bool = False) -> None:
            ignored = ignored_parent or bool(rel and path.name in IGNORED)
            if ignored and rel not in tracked and rel not in prefixes:
                return
            _entry(digest, path, str(root) + ('/' + rel if rel else ''), missing=True)
            if not path.is_symlink() and path.is_dir():
                for child in sorted(path.iterdir(), key=lambda p: os.fsencode(p.name)):
                    visit(child, f'{rel}/{child.name}' if rel else child.name, ignored)
        visit(root, '')
    return digest.hexdigest()


def atomic_json(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
