"""Invocation-only native hook configuration; never starts a model turn.

Tested with Codex CLI 0.171.0. hooks/list and hooks.state are native interfaces,
not a compatibility promise for other CLI versions. Trust covers the hook
definition, not the bytes of a local script: callers must freeze those separately.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import json
import math
import os
from pathlib import Path
import re
import selectors
import subprocess
import time

from .process import _stop


_EVENTS = {
    'PostToolUse': 'postToolUse', 'SessionStart': 'sessionStart',
    'PreModelRequest': 'preModelRequest', 'PostModelRequest': 'postModelRequest',
}
_SOURCE = '/<session-flags>/config.toml'


class HookConfigError(RuntimeError):
    """A failed preflight, with an optional hook-only audit (no full config)."""

    def __init__(self, message: str, metadata: dict | None = None):
        super().__init__(message)
        self.metadata = metadata or {}


def _toml(value) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if type(value) is bool:
        return 'true' if value else 'false'
    if type(value) is int:
        return str(value)
    if isinstance(value, Mapping):
        if not all(isinstance(k, str) for k in value):
            raise HookConfigError('TOML keys must be strings')
        return '{' + ','.join(_toml(k) + '=' + _toml(v) for k, v in value.items()) + '}'
    if isinstance(value, (list, tuple)):
        return '[' + ','.join(_toml(v) for v in value) + ']'
    raise HookConfigError('Unsupported hook configuration value')


def _definitions(definitions: Mapping) -> tuple[list[str], Counter]:
    if not isinstance(definitions, Mapping) or not definitions:
        raise HookConfigError('Provide a nonempty event-definition mapping')
    if not all(isinstance(key, str) for key in definitions):
        raise HookConfigError('Hook event names must be strings')
    flags, expected = [], Counter()
    for event in sorted(definitions):
        if event not in _EVENTS:
            raise HookConfigError('Only observational or context hook events are supported')
        groups = definitions[event]
        if not isinstance(groups, (list, tuple)) or not groups:
            raise HookConfigError('Each event needs nonempty matcher groups')
        normalized = []
        for group in groups:
            if not isinstance(group, Mapping) or set(group) - {'matcher', 'hooks'}:
                raise HookConfigError('Unsupported matcher group')
            matcher = group.get('matcher')
            if matcher is not None and (not isinstance(matcher, str) or len(matcher) > 256):
                raise HookConfigError('Invalid matcher')
            handlers = group.get('hooks')
            if not isinstance(handlers, (list, tuple)) or not handlers:
                raise HookConfigError('Each matcher group needs command hooks')
            clean_handlers = []
            for handler in handlers:
                allowed = {'type', 'command', 'timeout', 'async', 'additionalContextLimit', 'statusMessage'}
                if not isinstance(handler, Mapping) or set(handler) - allowed or handler.get('type') != 'command':
                    raise HookConfigError('Only bounded command hooks are supported')
                clean = dict(handler)
                command = clean.get('command')
                if not isinstance(command, str) or not command or '\x00' in command or len(command) > 8192:
                    raise HookConfigError('Invalid hook command')
                clean.setdefault('timeout', 2)
                if type(clean['timeout']) is not int or not 1 <= clean['timeout'] <= 10:
                    raise HookConfigError('Hook timeout must be 1..10 seconds')
                clean.setdefault('async', False)
                if clean['async'] is not False:
                    raise HookConfigError('Background hooks are unsupported by this preflight')
                limit = clean.get('additionalContextLimit')
                if limit is not None and event in ('PreModelRequest', 'PostModelRequest'):
                    raise HookConfigError('Diagnostic model-request hooks do not support context limits')
                if limit is not None and (type(limit) is not int or not 1 <= limit <= 512):
                    raise HookConfigError('Context limit must be 1..512 when provided')
                status = clean.get('statusMessage')
                if status is not None and (not isinstance(status, str) or len(status) > 200):
                    raise HookConfigError('Invalid hook status message')
                # Optional null values are represented by omission in TOML.
                clean = {k: v for k, v in clean.items() if v is not None}
                clean_handlers.append(clean)
                expected[(_EVENTS[event], command, matcher, clean['timeout'], False, limit, status)] += 1
            clean_group = {'hooks': clean_handlers}
            if matcher is not None:
                clean_group['matcher'] = matcher
            normalized.append(clean_group)
        flags.extend(['-c', 'hooks.' + event + '=' + _toml(normalized)])
    if sum(expected.values()) > 32 or sum(map(len, flags)) > 65536:
        raise HookConfigError('Hook configuration is too large')
    return flags, expected


def _native_probe(argv, flags, cwd, env, timeout_seconds):
    """Read native metadata with initialize/hooks/list/config/read only."""
    try:
        proc = subprocess.Popen([*argv, 'app-server', '--stdio', *flags], cwd=cwd,
                                env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=(os.name == 'posix'))
    except OSError as exc:
        raise HookConfigError('Cannot start native hook preflight: ' + type(exc).__name__) from exc
    selector = selectors.DefaultSelector()
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    total = 0
    deadline = time.monotonic() + timeout_seconds
    for name, stream in [('stdout', proc.stdout), ('stderr', proc.stderr)]:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)

    def send(message):
        proc.stdin.write((json.dumps(message) + '\n').encode())
        proc.stdin.flush()

    def rpc(request_id, method, params):
        nonlocal total
        send({'id': request_id, 'method': method, 'params': params})
        while time.monotonic() < deadline:
            while b'\n' in buffers['stdout']:
                raw, _, remaining = buffers['stdout'].partition(b'\n')
                buffers['stdout'] = bytearray(remaining)
                try:
                    message = json.loads(raw)
                except (UnicodeError, ValueError) as exc:
                    raise HookConfigError('Malformed native preflight JSON') from exc
                if not isinstance(message, dict):
                    raise HookConfigError('Malformed native preflight response')
                if message.get('id') == request_id:
                    if 'error' in message or 'result' not in message:
                        raise HookConfigError('Native preflight rejected ' + method)
                    return message['result']
                if 'method' in message and 'id' in message:
                    raise HookConfigError('Unexpected server request during no-model preflight')
            if not selector.get_map():
                raise HookConfigError('Native preflight exited before response')
            for key, _ in selector.select(min(.05, max(0, deadline - time.monotonic()))):
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if total > 2_000_000:
                    raise HookConfigError('Native preflight output exceeded limit')
                buffers[key.data].extend(chunk)
        raise HookConfigError('Native hook preflight timed out')

    try:
        identity = rpc(1, 'initialize', {'clientInfo': {'name': 'codex-compass-hook-preflight', 'version': '0.4'},
                                        'capabilities': {'experimentalApi': True}})
        send({'method': 'initialized'})
        result = rpc(2, 'hooks/list', {'cwds': [str(cwd)]})
        config = rpc(3, 'config/read', {'cwd': str(cwd), 'includeLayers': False})
        entries = result.get('data') if isinstance(result, dict) else None
        if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
            raise HookConfigError('Unsupported hooks/list response')
        entry = entries[0]
        if not isinstance(entry.get('hooks'), list) or not isinstance(entry.get('errors'), list) or not isinstance(entry.get('warnings'), list):
            raise HookConfigError('Incomplete hooks/list metadata')
        if not isinstance(config, dict) or not isinstance(config.get('config'), dict):
            raise HookConfigError('Unsupported config/read response')
        hooks_config = config['config'].get('hooks')
        if hooks_config is None:
            hooks_config = {}
        state = hooks_config.get('state') if isinstance(hooks_config, dict) else None
        if state is None:
            state = {}
        if not isinstance(hooks_config, dict) or not isinstance(state, dict):
            raise HookConfigError('Unsupported native hook trust state')
        if not all(isinstance(key, str) and isinstance(value, dict) for key, value in state.items()):
            raise HookConfigError('Unsupported native hook trust entry')
        return entry, state, identity
    except (BrokenPipeError, OSError) as exc:
        raise HookConfigError('Native preflight transport failed: ' + type(exc).__name__) from exc
    finally:
        _stop(proc)
        selector.close()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()


def _signature(hook):
    signature = (hook.get('eventName'), hook.get('command'), hook.get('matcher'),
                 hook.get('timeoutSec'), hook.get('async', False),
                 hook.get('additionalContextLimit'), hook.get('statusMessage'))
    if any(value is not None and type(value) not in (str, int, bool) for value in signature):
        raise HookConfigError('Malformed hook metadata fields')
    return signature


def prepare_hook_config(definitions: Mapping, cwd: Path, *,
                        codex_argv: Sequence[str] = ('codex',),
                        timeout_seconds: float = 10.0,
                        env: Mapping[str, str] | None = None,
                        reject_foreign: bool = True) -> dict:
    """Return config_flags and metadata, trusting only exact supplied definitions.

    No files/config/trust decisions are written by this helper. Native Codex may
    create its ordinary runtime files; pass an isolated CODEX_HOME for a study.
    Existing foreign hook state is copied unchanged and checked after override.
    reject_foreign rejects *all* foreign definitions, even already trusted ones.
    When false, conflicting inline definitions still fail rather than being
    replaced or silently moved to another trust identity.
    The caller must vet command bodies and independently freeze their file bytes.
    Successful metadata preflight proves neither hook delivery nor model behavior.
    """
    if isinstance(codex_argv, (str, bytes)) or not codex_argv or not all(isinstance(x, str) and x and '\x00' not in x for x in codex_argv):
        raise HookConfigError('codex_argv must be a nonempty argv sequence')
    if any(x.startswith('--dangerously-') for x in codex_argv):
        raise HookConfigError('Dangerous trust/sandbox bypass flags are not supported')
    if type(reject_foreign) is not bool:
        raise HookConfigError('reject_foreign must be boolean')
    if type(timeout_seconds) not in (float, int) or not 0 < timeout_seconds <= 120 or not math.isfinite(timeout_seconds):
        raise HookConfigError('Preflight timeout must be finite and 0..120 seconds')
    cwd = Path(cwd).resolve(strict=True)
    if not cwd.is_dir():
        raise HookConfigError('cwd must be a directory')
    flags, expected = _definitions(definitions)
    base_flags = ['-c', 'features.hooks=true']
    baseline, base_state, identity = _native_probe(codex_argv, base_flags, cwd, env, timeout_seconds)
    metadata = {'tested_cli_version': '0.171.0', 'native_identity': identity,
                'own_hooks': [], 'other_hooks': baseline['hooks'],
                'errors': baseline['errors'], 'warnings': baseline['warnings'],
                'preserved_state_count': len(base_state), 'no_model_turn_started': True,
                'local_script_bytes_covered_by_native_hash': False}
    if baseline['errors']:
        raise HookConfigError('Baseline hook configuration has errors', metadata)
    if reject_foreign and baseline['hooks']:
        raise HookConfigError('Foreign hook definitions are present', metadata)
    flags = base_flags + flags
    initial, state, _ = _native_probe(codex_argv, flags, cwd, env, timeout_seconds)
    if any(h not in initial['hooks'] for h in baseline['hooks']) or state != base_state:
        raise HookConfigError('Adding definitions would replace existing hooks or state', metadata)
    own, foreign, remaining = [], [], expected.copy()
    keys = set()
    for hook in initial['hooks']:
        if not isinstance(hook, dict) or not isinstance(hook.get('key'), str) or hook['key'] in keys:
            raise HookConfigError('Invalid or duplicate hook metadata')
        keys.add(hook['key'])
        signature = _signature(hook)
        if hook.get('source') == 'sessionFlags' and hook.get('sourcePath') == _SOURCE and remaining[signature] > 0:
            current_hash = hook.get('currentHash')
            if hook.get('handlerType') != 'command' or not isinstance(current_hash, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', current_hash):
                raise HookConfigError('Unsupported own hook hash or handler')
            if hook.get('isManaged') or not hook.get('enabled'):
                raise HookConfigError('Own hook is managed or disabled')
            remaining[signature] -= 1
            own.append(hook)
        else:
            foreign.append(hook)
    metadata.update(own_hooks=own, other_hooks=foreign, errors=initial['errors'],
                    warnings=initial['warnings'])
    if any(remaining.values()):
        raise HookConfigError('Native hooks do not match supplied definitions', metadata)
    if initial['errors']:
        raise HookConfigError('Native hook configuration has errors', metadata)
    if reject_foreign and foreign:
        raise HookConfigError('Foreign hook definitions are present', metadata)
    trust = dict(state)
    for hook in own:
        trust[hook['key']] = {**trust.get(hook['key'], {}), 'trusted_hash': hook['currentHash']}
    trusted_flags = flags + ['-c', 'hooks.state=' + _toml(trust)]
    verified, final_state, _ = _native_probe(codex_argv, trusted_flags, cwd, env, timeout_seconds)
    final_by_key = {h['key']: h for h in verified['hooks'] if isinstance(h, dict) and isinstance(h.get('key'), str)}
    if len(final_by_key) != len(verified['hooks']) or set(final_by_key) != keys or verified['errors']:
        raise HookConfigError('Native hook set changed during preflight', metadata)
    for hook in own:
        after = final_by_key[hook['key']]
        if after != {**hook, 'trustStatus': 'trusted'}:
            raise HookConfigError('Native exact-definition trust was not established', metadata)
    for hook in foreign:
        if final_by_key[hook['key']] != hook:
            raise HookConfigError('Foreign hook metadata changed during preflight', metadata)
    if final_state != trust:
        raise HookConfigError('Native trust state was not preserved exactly', metadata)
    metadata['own_hooks'] = [final_by_key[h['key']] for h in own]
    metadata['verified'] = True
    return {'config_flags': trusted_flags, 'metadata': metadata}
