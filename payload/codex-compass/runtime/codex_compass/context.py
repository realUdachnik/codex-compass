"""Bounded, fresh repository facts: a map and references, not a runtime graph.

No repository code is imported or executed. A packet can be incomplete and says
so. Hashes cover complete file bytes; oversized files are skipped, never silently
presented as fully scanned. POSIX no-follow reads keep symlinks outside the scope.
"""
from __future__ import annotations

import ast
from collections import Counter
import hashlib
import heapq
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
import tomllib

from .process import run_process

_GENERATED = {'.git', '.compass', '__pycache__', '.venv', 'venv', 'node_modules',
              '.pytest_cache', '.mypy_cache', '.ruff_cache', 'dist', 'build'}
_PRIVATE = {'private', '.ssh', '.aws', '.azure', '.secrets', 'secrets', '.credentials', '.env'}
_TEXT = {'.py', '.pyi', '.js', '.jsx', '.ts', '.tsx', '.go', '.rs', '.java', '.rb',
         '.sh', '.sql', '.md', '.rst', '.txt', '.toml', '.yaml', '.yml', '.json',
         '.ini', '.cfg', '.xml', '.html', '.css', '.vue', '.svelte', '.c', '.h', '.cpp'}
_CODE = {'.py', '.pyi', '.js', '.jsx', '.ts', '.tsx', '.go', '.rs', '.java', '.rb',
         '.sh', '.sql', '.html', '.css', '.vue', '.svelte', '.c', '.h', '.cpp'}
_CONFIG = {'pyproject.toml', 'package.json', 'tsconfig.json', 'pytest.ini',
           'setup.cfg', 'setup.py', 'tox.ini', 'Makefile', 'Dockerfile',
           'Cargo.toml', 'go.mod', 'requirements.txt', 'justfile'}
_SECRET_NAME = re.compile(r'(^\.env(?:\.|$)|(?:^|[._-])(?:credentials?|secrets?|tokens?|passwords?)(?:[._-]|$)|^id_(?:rsa|ed25519)|\.(?:pem|key|p12|pfx|session)$)', re.I)
_TOKEN = re.compile(r'[A-Za-z_][A-Za-z_0-9]{2,}')
_STOP = {'the', 'and', 'for', 'with', 'from', 'that', 'this', 'tests', 'test',
         'file', 'files', 'code', 'change', 'update', 'implement', 'fix', 'none', 'true', 'false'}


def _kind(path: str) -> str:
    p = PurePosixPath(path)
    parts = {x.lower() for x in p.parts}
    name = p.name.lower()
    if name.startswith(('test_', 'test.')) or name.endswith(('_test.py', '.test.ts', '.spec.ts', '_test.go')) or parts & {'tests', 'test', '__tests__'}:
        return 'test'
    if parts & {'examples', 'example', 'samples', 'sample', 'demos'}:
        return 'example'
    if p.name in _CONFIG or p.suffix.lower() in {'.toml', '.yaml', '.yml', '.ini', '.cfg'} or 'config' in name:
        return 'config'
    if p.suffix.lower() in {'.md', '.rst', '.txt'}:
        return 'doc'
    return 'source'


def _eligible(path: str, tracked: bool = False) -> str | None:
    p = PurePosixPath(path)
    if p.is_absolute() or '..' in p.parts or not p.parts:
        return 'outside_workspace'
    if any(x.lower() in _PRIVATE for x in p.parts):
        return 'private_or_secret'
    # Credential-handling source modules are useful code, not serialized secrets.
    if re.match(r'^\.env(?:\.|$)', p.name, re.I) or p.suffix.lower() in {'.pem', '.key', '.p12', '.pfx', '.session'} or p.name.startswith(('id_rsa', 'id_ed25519')):
        return 'private_or_secret'
    if _SECRET_NAME.search(p.name) and p.suffix.lower() not in _CODE:
        return 'private_or_secret'
    if any(x in _GENERATED for x in p.parts) and not tracked:
        return 'generated'
    if p.suffix.lower() not in _TEXT and p.name not in _CONFIG and p.name != 'AGENTS.md':
        return 'non_text_type'
    return None


def _read(root: Path, path: str, limit: int) -> bytes:
    """Open each component relative to a pinned directory, never follow links."""
    handles = []
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        handles.append(directory)
        parts = PurePosixPath(path).parts
        for part in parts[:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            handles.append(directory)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        handles.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('not_regular')
        if before.st_size > limit:
            raise ValueError('file_byte_limit')
        with os.fdopen(os.dup(fd), 'rb') as stream:
            data = stream.read(limit + 1)
        after = os.fstat(fd)
        if len(data) > limit:
            raise ValueError('file_byte_limit')
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('changed_during_read')
        return data
    finally:
        for fd in reversed(handles):
            os.close(fd)


def _modules(path: str) -> list[str]:
    parts = list(PurePosixPath(path).with_suffix('').parts)
    if parts[-1] == '__init__':
        parts.pop()
    names = ['.'.join(parts)] if parts else []
    if parts and parts[0] in {'src', 'lib', 'python'}:
        names.append('.'.join(parts[1:]))
    return [x for x in names if x]


def build_context(workspace: Path, goal: str = '', seeds: tuple[str, ...] = (), *,
                  max_files: int = 400, max_bytes: int = 2000000,
                  max_chars: int = 18000, max_seconds: float = 2.0) -> dict:
    for name, value in [('max_files', max_files), ('max_bytes', max_bytes), ('max_chars', max_chars)]:
        if type(value) is not int or value < (1024 if name == 'max_chars' else 1):
            raise ValueError(name + ' must be a positive integer (max_chars >= 1024)')
    if isinstance(max_seconds, bool) or not isinstance(max_seconds, (float, int)) or max_seconds <= 0 or max_seconds > 86400 or (isinstance(max_seconds, float) and not math.isfinite(max_seconds)):
        raise ValueError('max_seconds must be finite and positive')
    if not isinstance(goal, str):
        raise ValueError('goal must be a string')
    root = Path(workspace).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('workspace must be a directory')
    start = time.monotonic()
    deadline = start + max_seconds
    skipped = Counter()
    reasons = set()
    if len(goal) > 24000:
        goal = goal[:24000]
        reasons.add('goal_character_limit')
    coverage = dict(files_listed=0, files_read=0, bytes_read=0, elapsed_seconds=0.0,
                    truncated=False, reasons=[], skipped={}, gitignore_mode='not_scanned')
    result = dict(schema_version=1, workspace=str(root), seeds=[], map=[], facts=[], coverage=coverage,
                  limitations=['Static imports and lexical references are candidates, not runtime callers or a complete impact graph.',
                               'The map covers only eligible local text files within the reported scan budget; no prior chats or external memory are searched.'])
    def expired() -> bool:
        if time.monotonic() >= deadline:
            reasons.add('max_seconds')
            return True
        return False
    inventory = {}
    inventory_cap = max(64, min(max_files * 10, 20000))
    # git selects tracked files and nonignored additions, including tracked code
    # under conventional generated names. A capped process prevents huge output.
    if not expired():
        command = ['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others', '--exclude-standard']
        listed = run_process(command, root, min(max(0.001, deadline - time.monotonic()), 0.7), 262144)
        if listed['returncode'] == 0 and not listed['timed_out'] and not listed['spawn_error']:
            coverage['gitignore_mode'] = 'git_ls_files'
            if listed['output_truncated']:
                reasons.add('inventory_output_limit')
            paths = listed['stdout'].split('\0')[:-1]
            # ls-files alone does not label tracked vs untracked. A second bounded
            # index query is needed only for conventional generated candidates.
            tracked = set()
            if any(any(x in _GENERATED for x in PurePosixPath(p).parts) for p in paths) and not expired():
                index = run_process(['git', '-C', str(root), 'ls-files', '-z', '--cached'], root,
                                    min(max(0.001, deadline-time.monotonic()), 0.5), 262144)
                if index['returncode'] == 0 and not index['output_truncated']:
                    tracked = set(index['stdout'].split('\0')[:-1])
                else:
                    reasons.add('tracked_inventory_incomplete')
            for path in paths:
                if expired():
                    break
                coverage['files_listed'] += 1
                why = _eligible(path, path in tracked)
                if why:
                    skipped[why] += 1
                else:
                    inventory[path] = path in tracked
                if coverage['files_listed'] >= inventory_cap:
                    reasons.add('inventory_file_limit')
                    break
        else:
            coverage['gitignore_mode'] = 'fallback_walk_no_gitignore'
    if coverage['gitignore_mode'] == 'fallback_walk_no_gitignore' and not expired():
        stack = [root]
        directories = 0
        while stack and not expired():
            directory = stack.pop()
            directories += 1
            if directories > inventory_cap:
                reasons.add('inventory_directory_limit')
                break
            try:
                children = sorted(directory.iterdir(), key=lambda p: p.name)
            except OSError:
                skipped['unreadable_directory'] += 1
                continue
            for child in children:
                if expired():
                    break
                rel = child.relative_to(root).as_posix()
                if child.is_symlink():
                    skipped['symlink'] += 1
                    continue
                if child.is_dir():
                    if child.name in _GENERATED or child.name.lower() in _PRIVATE :
                        skipped['generated_or_private_directory'] += 1
                    else:
                        stack.append(child)
                    continue
                coverage['files_listed'] += 1
                why = _eligible(rel)
                if why:
                    skipped[why] += 1
                else:
                    inventory[rel] = False
                if coverage['files_listed'] >= inventory_cap:
                    reasons.add('inventory_file_limit')
                    stack.clear()
                    break
    selected = {}
    seed_reasons = {}
    for index, seed in enumerate(seeds):
        if expired():
            break
        if index >= 64:
            reasons.add('seed_limit')
            break
        if not isinstance(seed, str):
            raise ValueError('seeds must contain paths as strings')
        raw = re.sub(r':\d+(?::\d+)?$', '', seed)
        p = Path(raw)
        if p.is_absolute():
            try:
                raw = p.relative_to(root).as_posix()
            except ValueError:
                skipped['outside_seed'] += 1
                continue
        raw = PurePosixPath(raw).as_posix()
        why = _eligible(raw, inventory.get(raw, False))
        if why:
            skipped['rejected_seed'] += 1
            continue
        if raw in inventory:
            seed_reasons[raw] = 'explicit_seed'
        else:
            skipped['seed_not_in_inventory'] += 1
    # Goals and traceback strings can name either an absolute or relative path.
    for path in inventory:
        if expired():
            break
        name = PurePosixPath(path).name
        if path in goal or str(root / path) in goal or (len(name) >= 5 and re.search(r'(?<![\w.])' + re.escape(name) + r'(?![\w.])', goal)):
            seed_reasons.setdefault(path, 'goal_or_failure_path')
    terms = {x for x in _TOKEN.findall(goal) if x.lower() not in _STOP}
    kind_ranks = {}
    kind_counts = Counter()
    for path in sorted(inventory):
        group = (_kind(path), path.split('/')[0] if '/' in path else '.')
        kind_ranks[path] = kind_counts[group]
        kind_counts[group] += 1
    entry_paths, seed_neighbors = set(), set()
    def priority(path: str):
        p = PurePosixPath(path)
        tier = (0 if path in seed_reasons else 1 if '/' not in path and _kind(path) == 'config' else
                2 if path in entry_paths else 3 if path in seed_neighbors else
                4 if p.name in {'cli.py', '__main__.py', 'main.py', 'app.py', 'manage.py'} else
                5 if p.stem in terms else 6)
        if tier > 0 and p.name == '__init__.py' and path in selected and not selected[path][0]['top_defs']:
            tier = 7
        same_seed_folder = any(path.split('/')[0] == seed.split('/')[0] for seed in seed_reasons)
        return (tier, 0 if tier != 3 or same_seed_folder else 1,
                {'config': 0, 'source': 1, 'test': 2, 'example': 3, 'doc': 4}[_kind(path)] if tier == 3 else 0,
                kind_ranks[path] // 2,
                {'config': 0, 'source': 1, 'test': 2, 'example': 3, 'doc': 4}[_kind(path)], path)
    for path in sorted(inventory, key=priority):
        if expired():
            break
        if coverage['files_read'] >= max_files:
            reasons.add('max_files')
            break
        remaining = max_bytes - coverage['bytes_read']
        if remaining <= 0:
            reasons.add('max_bytes')
            break
        try:
            raw = _read(root, path, min(remaining, 131072))
        except ValueError as exc:
            skipped[str(exc)] += 1
            reasons.add('skipped_file_bytes' if str(exc) == 'file_byte_limit' else 'read_incomplete')
            continue
        except OSError:
            skipped['unreadable_or_symlink'] += 1
            continue
        coverage['files_read'] += 1
        coverage['bytes_read'] += len(raw)
        if b'\0' in raw:
            skipped['binary'] += 1
            continue
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            skipped['non_utf8'] += 1
            continue
        item = dict(path=path, kind=_kind(path), sha256=hashlib.sha256(raw).hexdigest(), top_defs=[])
        tree = None
        if PurePosixPath(path).suffix == '.py':
            try:
                tree = ast.parse(text)
                item['top_defs'] = [{'name': n.name, 'line': n.lineno} for n in tree.body
                                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and len(n.name) <= 200][:16]
            except (SyntaxError, ValueError, RecursionError):
                skipped['python_parse_failed'] += 1
        selected[path] = (item, text, tree)
        if any(d['name'] in terms for d in item['top_defs']):
            seed_reasons.setdefault(path, 'goal_symbol_definition')
    result['seeds'] = [{'path': p, 'reason': why} for p, why in seed_reasons.items() if p in selected]
    module_paths = {}
    for path in selected:
        if PurePosixPath(path).suffix == '.py':
            for module in _modules(path):
                module_paths.setdefault(module, set()).add(path)
    if 'pyproject.toml' in selected:
        try:
            scripts = tomllib.loads(selected['pyproject.toml'][1]).get('project', {}).get('scripts', {})
            for value in scripts.values():
                if isinstance(value, str):
                    for path in module_paths.get(value.split(':')[0], set()):
                        entry_paths.add(path)
                        selected[path][0]['reason'] = 'Declared project.scripts entrypoint'
        except (tomllib.TOMLDecodeError, AttributeError, TypeError):
            skipped['entrypoint_parse_failed'] += 1
    facts = []
    fact_sequence = 0
    fact_limit = min(256, max(16, max_chars // 80))
    source_lines = {path: text.splitlines() for path, (_, text, _) in selected.items()}
    def fact(path, line, kind, reason, related_path=None, score=0):
        nonlocal fact_sequence
        item, text, _ = selected[path]
        lines = source_lines[path]
        if not 1 <= line <= len(lines):
            return
        full_snippet = '\n'.join(lines[line-1:line+1])
        snippet = full_snippet[:600]
        value = dict(path=path, line=line, kind=kind, snippet=snippet,
                     sha256=item['sha256'], reason=reason)
        if len(full_snippet) > 600:
            value['snippet_truncated'] = True
        if related_path is not None:
            value['related_path'] = related_path
        fact_sequence += 1
        entry = (score, -fact_sequence, value)
        if len(facts) < fact_limit:
            heapq.heappush(facts, entry)
        else:
            reasons.add('fact_limit')
            if entry[:2] > facts[0][:2]:
                heapq.heapreplace(facts, entry)
    for path, (item, text, tree) in selected.items():
        if expired():
            break
        kind = item['kind']
        if kind in {'test', 'config', 'example', 'doc'}:
            fact(path, 1, kind + '_candidate', 'Existing local ' + kind + '; relevance needs inspection', score=10)
        if path in seed_reasons:
            for definition in item['top_defs'][:4]:
                fact(path, definition['line'], 'definition', 'Definition in a selected seed file', score=80)
        if tree is None:
            continue
        modules = _modules(path)
        canonical = modules[-1] if modules else ''
        package = canonical if PurePosixPath(path).name == '__init__.py' else canonical.rpartition('.')[0]
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            targets = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            else:
                base = node.module or ''
                if node.level:
                    parts = package.split('.') if package else []
                    if node.level > len(parts):
                        continue
                    anchor = parts[:len(parts)-(node.level-1)]
                    base = '.'.join(anchor + ([base] if base else []))
                targets = [base] + [base + '.' + alias.name for alias in node.names if base]
            for target in dict.fromkeys(targets):
                matches = module_paths.get(target, set())
                if len(matches) == 1:
                    related = next(iter(matches))
                    if related != path:
                        if related in seed_reasons or path in seed_reasons:
                            seed_neighbors.update((path, related))
                        fact(path, node.lineno, 'import_reference', 'Syntactic local import; not a runtime caller claim', related,
                             score=100 if related in seed_reasons else 60 if path in seed_reasons else 20)
    reference_terms = set()
    for path in seed_reasons:
        if path not in selected:
            continue
        reference_terms.add(PurePosixPath(path).stem)
        reference_terms.update(_modules(path))
        reference_terms.update(d['name'] for d in selected[path][0]['top_defs'])
    reference_terms = sorted((x for x in reference_terms if len(x) >= 3 and x not in _STOP), key=lambda x: (-len(x), x))[:24]
    reference_patterns = [re.compile(r'(?<![\w])' + re.escape(term) + r'(?![\w])') for term in reference_terms]
    for path, (_, text, _) in selected.items():
        if expired():
            break
        hits = 0
        for number, line in enumerate(source_lines[path], 1):
            if number % 64 == 0 and expired():
                break
            if any(pattern.search(line) for pattern in reference_patterns):
                if path not in seed_reasons:
                    fact(path, number, 'lexical_reference', 'Exact seed module/symbol text; relevance is not proven', score=50)
                    hits += 1
                    if hits >= 3:
                        break
    result['map'] = [dict(selected[p][0], top_defs=selected[p][0]['top_defs'][:6]) for p in sorted(selected, key=priority)]
    seen = set()
    for _, _, value in sorted(facts, key=lambda entry: (-entry[0], -entry[1])):
        key = (value['path'], value['line'], value['kind'], value.get('related_path'))
        if key not in seen:
            result['facts'].append(value)
            seen.add(key)
    # Fit independently useful map and evidence into one explicit character cap.
    def size():
        return len(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
    if size() > max_chars:
        reasons.add('max_chars')
        while result['map'] and len(json.dumps(result['map'], ensure_ascii=False)) > max_chars * .35:
            result['map'].pop()
        while size() > max_chars - 200 and result['facts']:
            result['facts'].pop()
        while size() > max_chars - 200 and result['map']:
            result['map'].pop()
        while size() > max_chars - 200 and result['seeds']:
            result['seeds'].pop()
    coverage.update(elapsed_seconds=round(time.monotonic()-start, 6), truncated=bool(reasons),
                    reasons=sorted(reasons), skipped=dict(sorted(skipped.items())))
    while size() > max_chars and result['facts']:
        result['facts'].pop()
    while size() > max_chars and result['map']:
        result['map'].pop()
    while size() > max_chars and result['seeds']:
        result['seeds'].pop()
    if size() > max_chars:
        result['limitations'] = ['Context omitted because metadata itself exceeded max_chars.']
        result['workspace'] = '<omitted: character limit>'
        coverage['truncated'] = True
        coverage['reasons'] = ['max_chars']
    return result
