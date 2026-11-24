"""Bounded subprocess execution. POSIX process groups are cleanup, not a sandbox."""
from __future__ import annotations

import os
from pathlib import Path
import selectors
import signal
import subprocess
import time


def _stop(proc: subprocess.Popen) -> None:
    if os.name == 'posix':
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        # A dead leader does not imply that its descendants are dead.
        end = time.monotonic() + 0.15
        while time.monotonic() < end:
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=0.5)


def run_process(argv: list[str], cwd: Path, timeout_seconds: float,
                max_output_bytes: int = 131072,
                input_text: str | None = None) -> dict:
    """Drain both pipes concurrently, retain at most max_output_bytes in total.

    Children that deliberately escape the process group require an OS sandbox.
    The output cap does not stop execution: the pipes keep draining to avoid a
    deadlock. A bounded cleanup grace may extend the execution timeout slightly.
    """
    if not argv or not isinstance(argv[0], str) or not argv[0] or not all(isinstance(x, str) and '\x00' not in x for x in argv):
        raise ValueError('argv must contain strings and a nonempty executable')
    if timeout_seconds <= 0 or max_output_bytes < 1:
        raise ValueError('timeout and output limit must be positive')
    start = time.monotonic()
    result = dict(argv=list(argv), cwd=str(cwd), returncode=None, stdout='',
                  stderr='', timed_out=False, output_truncated=False,
                  duration_seconds=0.0, spawn_error=None)
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=(os.name == 'posix'),
                                shell=False, close_fds=True)
    except (OSError, ValueError) as exc:
        # Do not include arbitrary exception text (it can contain URLs/secrets).
        result['spawn_error'] = type(exc).__name__ + (f': errno={exc.errno}' if isinstance(exc, OSError) else '')
        result['duration_seconds'] = time.monotonic() - start
        return result
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    remaining = max_output_bytes
    payload = memoryview((input_text or '').encode('utf-8'))
    selector = selectors.DefaultSelector()
    for name, stream in (('stdout', proc.stdout), ('stderr', proc.stderr)):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    if payload:
        os.set_blocking(proc.stdin.fileno(), False)
        selector.register(proc.stdin, selectors.EVENT_WRITE, 'stdin')
    else:
        proc.stdin.close()
    cleanup_deadline = None
    try:
        while selector.get_map() or proc.poll() is None:
            now = time.monotonic()
            if cleanup_deadline is None and now - start >= timeout_seconds:
                result['timed_out'] = True
                _stop(proc)
                cleanup_deadline = time.monotonic() + 0.3
            elif cleanup_deadline is None and proc.poll() is not None:
                _stop(proc)
                cleanup_deadline = time.monotonic() + 0.3
            if cleanup_deadline is not None and time.monotonic() >= cleanup_deadline:
                break
            for key, _ in selector.select(0.02):
                stream, name = key.fileobj, key.data
                if name == 'stdin':
                    try:
                        sent = os.write(stream.fileno(), payload[:65536])
                        payload = payload[sent:]
                    except (BrokenPipeError, OSError):
                        payload = payload[0:0]
                    if not payload:
                        selector.unregister(stream)
                        stream.close()
                    continue
                try:
                    chunk = os.read(stream.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                else:
                    kept = chunk[:remaining]
                    buffers[name].extend(kept)
                    remaining -= len(kept)
                    if len(kept) != len(chunk):
                        result['output_truncated'] = True
        result['returncode'] = proc.poll()
    except BaseException:
        _stop(proc)
        raise
    finally:
        _stop(proc)
        selector.close()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None and not stream.closed:
                stream.close()
    for name, data in buffers.items():
        result[name] = data.decode('utf-8', errors='replace')
    result['duration_seconds'] = time.monotonic() - start
    return result
