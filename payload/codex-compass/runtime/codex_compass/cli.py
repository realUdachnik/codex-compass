"""Command line interface; no global configuration changes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import uuid

from . import __version__
from .contracts import ContractError, example_contract, load_contract


EXIT = {'checks_passed': 0, 'checks_failed': 1, 'stalled': 3,
        'budget_exhausted': 4, 'attempts_exhausted': 4,
        'protected_changed': 5, 'evidence_stale': 5, 'infrastructure_error': 6}


def _json(data):
    print(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))


def _run_dir(contract, supplied: str | None) -> Path:
    if supplied:
        return Path(supplied).expanduser().resolve()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    return contract.workspace / '.compass' / 'runs' / f'{stamp}-{uuid.uuid4().hex[:8]}'


def _doctor() -> dict:
    codex = shutil.which('codex')
    version = None
    if codex:
        try:
            p = subprocess.run([codex, '--version'], capture_output=True, text=True, timeout=10)
            version = p.stdout.strip()[:200] if p.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {'compass_version': __version__, 'python': sys.version.split()[0],
            'platform': sys.platform, 'codex_path': codex, 'codex_version': version,
            'process_groups_supported': os.name == 'posix',
            'auth': 'not checked; a real run uses existing Codex authentication',
            'ready': bool(codex and version and os.name == 'posix')}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='codex-compass', description='Run Codex with bounded retries and external acceptance evidence.')
    parser.add_argument('--version', action='version', version=__version__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor', help='Check local prerequisites without model calls')
    assist = sub.add_parser('assist', help='Run one Codex task with bounded native reminders')
    assist.add_argument('--workspace', '-C', default='.')
    assist.add_argument('--goal', required=True)
    assist.add_argument('--model', help='Omit to use existing Codex CLI model settings')
    assist.add_argument('--reasoning', choices=['low','medium','high','xhigh','max'])
    assist.add_argument('--seconds', type=int, default=1800)
    assist.add_argument('--run-dir')
    context = sub.add_parser('context', help='Inspect a bounded repository map and grounded references without a task contract')
    context.add_argument('--workspace', '-C', default='.')
    context.add_argument('--goal', default='')
    context.add_argument('--seed', action='append', default=[], help='A file discovered by the agent; repeat for several seeds')
    context.add_argument('--max-files', type=int, default=400)
    context.add_argument('--max-bytes', type=int, default=2000000)
    context.add_argument('--max-chars', type=int, default=18000)
    context.add_argument('--max-seconds', type=float, default=2.0)
    init = sub.add_parser('init', help='Write a task contract with explicit acceptance commands')
    init.add_argument('path')
    init.add_argument('--goal', required=True)
    init.add_argument('--workspace', default='.', help='Path relative to contract directory')
    init.add_argument('--check', action='append', required=True, help='Quoted argv; split with shlex, never run by a shell. Repeat for several checks.')
    for name in ('run', 'check'):
        p = sub.add_parser(name, help='Run Codex and verify' if name == 'run' else 'Run acceptance checks without a model call')
        p.add_argument('contract')
        p.add_argument('--run-dir', help='Directory for evidence; defaults to workspace/.compass/runs/<id>')
        if name == 'run':
            p.add_argument('--resume', action='store_true', help='Resume this --run-dir; recheck evidence. Time budget restarts; attempt count persists.')
    status = sub.add_parser('status', help='Read a saved report without claiming it is still current')
    status.add_argument('run_dir')
    args = parser.parse_args(argv)
    try:
        if args.command == 'assist':
            from .assist import run_assisted
            result = run_assisted(args.workspace, args.goal, model=args.model, reasoning=args.reasoning,
                                  seconds=args.seconds, run_dir=args.run_dir)
            _json(result)
            return 0 if result['status']=='completed' else 4 if result['status']=='time_limit' else 6
        if args.command == 'context':
            from .context import build_context
            result = build_context(Path(args.workspace).expanduser().resolve(), args.goal, tuple(args.seed),
                                   max_files=args.max_files, max_bytes=args.max_bytes, max_chars=args.max_chars, max_seconds=args.max_seconds)
            _json(result)
            return 0
        if args.command == 'doctor':
            result = _doctor()
            _json(result)
            return 0 if result['ready'] else 6
        if args.command == 'init':
            path = Path(args.path).expanduser().resolve()
            checks = [shlex.split(command) for command in args.check]
            if any(not command for command in checks):
                raise ContractError('each --check must contain an executable')
            data = example_contract(args.goal, args.workspace, checks)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation never overwrites an existing task.
            with path.open('x', encoding='utf-8') as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
            try:
                load_contract(path)
            except ContractError:
                path.unlink()
                raise
            _json({'contract': str(path), 'next': f'codex-compass check {shlex.quote(str(path))}'})
            return 0
        if args.command == 'status':
            path = Path(args.run_dir).expanduser().resolve()
            data = json.loads((path / 'report.json').read_text())
            _json({'saved_report': data, 'freshness': 'historical evidence; use check or run --resume to revalidate'})
            return 0
        contract = load_contract(args.contract)
        if args.command == 'run' and args.resume and not args.run_dir:
            raise ContractError('--resume requires --run-dir')
        directory = _run_dir(contract, args.run_dir)
        from .engine import run, verify
        if args.command == 'check':
            result = verify(contract, directory)
        else:
            result = run(contract, directory, resume=args.resume)
        _json({'run_dir': str(directory), **result})
        return EXIT.get(result.get('status'), 1)
    except (ContractError, ValueError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        _json({'status': 'error', 'error': str(exc)})
        return 2
    except KeyboardInterrupt:
        _json({'status': 'interrupted', 'reason': 'Stopped by user; subprocess group terminated.'})
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
