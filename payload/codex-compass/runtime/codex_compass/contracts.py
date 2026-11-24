"""Strict, immutable task contracts. Commands are argv, never shell strings."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any


class ContractError(ValueError):
    """A task contract cannot be executed as written."""


@dataclass(frozen=True)
class Check:
    name: str
    argv: tuple[str, ...]
    cwd: Path
    timeout_seconds: float
    success_pattern: str | None = None


@dataclass(frozen=True)
class Limits:
    max_attempts: int = 3
    total_seconds: float = 600
    attempt_seconds: float = 180
    stall_attempts: int = 2
    max_output_bytes: int = 131072


@dataclass(frozen=True)
class Contract:
    path: Path
    workspace: Path
    goal: str
    checks: tuple[Check, ...]
    limits: Limits
    protected_paths: tuple[Path, ...]
    digest: str


def _object(value: Any, label: str, allowed: set[str], required: set[str] | None = None) -> dict:
    if not isinstance(value, dict):
        raise ContractError(f'{label} must be an object')
    extra = set(value) - allowed
    if extra:
        raise ContractError(f'{label}: unknown fields: {", ".join(sorted(extra))}')
    missing = (required or set()) - set(value)
    if missing:
        raise ContractError(f'{label}: missing fields: {", ".join(sorted(missing))}')
    return value


def _string(value: Any, label: str, *, maximum: int = 100000) -> str:
    if not isinstance(value, str) or not value.strip() or '\x00' in value or len(value) > maximum:
        raise ContractError(f'{label} must be a nonempty string without NUL, at most {maximum} characters')
    return value


def _number(value: Any, label: str, *, integer: bool = False, minimum: float = 0, maximum: float = 86400) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(value, float) and not math.isfinite(value)):
        raise ContractError(f'{label} must be a finite number')
    if integer and not isinstance(value, int):
        raise ContractError(f'{label} must be an integer')
    if value < minimum or value > maximum:
        raise ContractError(f'{label} must be between {minimum} and {maximum}')
    return value


def _within(path: Path, root: Path, label: str) -> Path:
    if not path.is_relative_to(root):
        raise ContractError(f'{label} must stay inside workspace: {path}')
    return path


def load_contract(path: str | Path) -> Contract:
    """Load v1 JSON; resolve workspace against contract location and cwd against workspace."""
    path = Path(path).expanduser().resolve()
    try:
        raw = path.read_bytes()
        if len(raw) > 1_048_576:
            raise ContractError('contract exceeds 1 MiB')
        def no_duplicates(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ContractError(f'duplicate JSON field: {key}')
                result[key] = value
            return result
        data = json.loads(raw, object_pairs_hook=no_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f'cannot read contract: {type(exc).__name__}') from exc
    _object(data, 'contract', {'schema_version', 'workspace', 'goal', 'checks', 'limits', 'protected_paths'}, {'schema_version', 'goal', 'checks'})
    if type(data['schema_version']) is not int or data['schema_version'] != 1:
        raise ContractError('schema_version must be 1')
    workspace_value = _string(data.get('workspace', '.'), 'workspace', maximum=4096)
    workspace = (path.parent / Path(workspace_value).expanduser()).resolve()
    if not workspace.is_dir():
        raise ContractError(f'workspace is not a directory: {workspace}')
    goal = _string(data['goal'], 'goal')
    raw_checks = data['checks']
    if not isinstance(raw_checks, list) or not 1 <= len(raw_checks) <= 64:
        raise ContractError('checks must contain 1 to 64 checks')
    checks = []
    names = set()
    for index, item in enumerate(raw_checks):
        label = f'checks[{index}]'
        _object(item, label, {'name', 'argv', 'cwd', 'timeout_seconds', 'success_pattern'}, {'name', 'argv'})
        name = _string(item['name'], label + '.name', maximum=200)
        if name in names:
            raise ContractError('check names must be unique')
        names.add(name)
        argv = item['argv']
        if not isinstance(argv, list) or not 1 <= len(argv) <= 256:
            raise ContractError(label + '.argv must be a nonempty array (not a shell command)')
        for arg_index, arg in enumerate(argv):
            # Empty arguments are meaningful; only the executable must be nonempty.
            if not isinstance(arg, str) or '\x00' in arg or len(arg) > 100000 or (arg_index == 0 and not arg.strip()):
                raise ContractError(label + '.argv contains an invalid argument')
        cwd = _within((workspace / _string(item.get('cwd', '.'), label + '.cwd', maximum=4096)).resolve(), workspace, label + '.cwd')
        if not cwd.is_dir():
            raise ContractError(label + '.cwd must be an existing directory')
        timeout = _number(item.get('timeout_seconds', 30), label + '.timeout_seconds', minimum=0.01)
        pattern = item.get('success_pattern')
        if pattern is not None:
            _string(pattern, label + '.success_pattern', maximum=4096)
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ContractError(label + '.success_pattern is not a valid regular expression') from exc
        checks.append(Check(name, tuple(argv), cwd, float(timeout), pattern))
    raw_limits = data.get('limits', {})
    fields = {'max_attempts', 'total_seconds', 'attempt_seconds', 'stall_attempts', 'max_output_bytes'}
    _object(raw_limits, 'limits', fields)
    limits = Limits(
        max_attempts=int(_number(raw_limits.get('max_attempts', 3), 'max_attempts', integer=True, minimum=1, maximum=100)),
        total_seconds=float(_number(raw_limits.get('total_seconds', 600), 'total_seconds', minimum=0.01)),
        attempt_seconds=float(_number(raw_limits.get('attempt_seconds', 180), 'attempt_seconds', minimum=0.01)),
        stall_attempts=int(_number(raw_limits.get('stall_attempts', 2), 'stall_attempts', integer=True, minimum=1, maximum=100)),
        max_output_bytes=int(_number(raw_limits.get('max_output_bytes', 131072), 'max_output_bytes', integer=True, minimum=1024, maximum=16_777_216)),
    )
    protected_values = data.get('protected_paths', [])
    if not isinstance(protected_values, list) or len(protected_values) > 256:
        raise ContractError('protected_paths must be an array of at most 256 paths')
    protected = []
    for value in protected_values:
        supplied = workspace / _string(value, 'protected path', maximum=4096)
        lexical = Path(os.path.abspath(supplied))
        p = _within(supplied.resolve(), workspace, 'protected path')
        cursor = lexical
        while cursor != workspace and cursor != cursor.parent:
            if cursor.is_symlink():
                raise ContractError('protected paths must not contain symlinks; protect regular files/directories')
            cursor = cursor.parent
        if p.is_dir():
            for folder, directories, files in os.walk(p, followlinks=False):
                if any((Path(folder) / child).is_symlink() for child in directories + files):
                    raise ContractError('protected directories must not contain symlinks')
        if not p.exists():
            raise ContractError(f'protected path does not exist: {p}')
        if p == workspace:
            raise ContractError('protect specific files/directories, not the whole editable workspace')
        protected.append(p)
    # Includes normalized paths, so copying a contract into a new directory does not
    # accidentally resume another workspace's evidence.
    canonical = {'contract': data, 'workspace': str(workspace), 'path': str(path)}
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return Contract(path, workspace, goal, tuple(checks), limits, tuple(dict.fromkeys(protected)), digest)


def example_contract(goal: str, workspace: str, checks: list[list[str]]) -> dict:
    return {
        'schema_version': 1,
        'workspace': workspace,
        'goal': goal,
        'checks': [{'name': f'acceptance-{i + 1}', 'argv': argv, 'timeout_seconds': 30} for i, argv in enumerate(checks)],
        'limits': {'max_attempts': 3, 'total_seconds': 600, 'attempt_seconds': 180, 'stall_attempts': 2},
        'protected_paths': [],
    }
