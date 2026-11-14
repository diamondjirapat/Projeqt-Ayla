#!/usr/bin/env python3
"""Run the backend with prefixed logs and automatic restarts.

This runner owns only the bot process and its children. Frontend and NodeLink
must be launched independently from their own repositories.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import TextIO

# ANSI color codes
RESET = '\033[0m'
COLORS = {
    'bot': '\033[32m',  # Green
    'runner': '\033[34m',  # Blue
}

LOG_DIR_NAME = 'logs'


def resolve_dated_log_path(root: Path | None = None, on_date: date | None = None) -> Path:
    """Resolve a per-run unified log file inside the ``logs/`` directory.

    The file is named ``DD-MM-YYYY-N.log`` where ``N`` is the run counter for
    that date (1 for the first run of the day, incrementing while the name is
    taken) so restarts on the same day never overwrite each other. The
    directory is created when missing. Mirrors
    ``utils.helpers.resolve_dated_log_path`` so direct bot runs and runner
    runs share the same naming scheme; it is duplicated here to keep the
    runner dependent on the standard library only.
    """
    base = (root or Path(__file__).resolve().parents[1]) / LOG_DIR_NAME
    base.mkdir(parents=True, exist_ok=True)
    prefix = (on_date or date.today()).strftime('%d-%m-%Y')
    run = 1
    while (base / f'{prefix}-{run}.log').exists():
        run += 1
    return base / f'{prefix}-{run}.log'


# Seconds to wait before restarting a service that exited unexpectedly.
RESTART_DELAY_S = 3.0
# A restart counts as a fast fail when the service lived less than this long.
FAST_FAIL_WINDOW_S = 10.0
# Shut everything down after this many consecutive fast fails (crash-loop guard).
MAX_CONSECUTIVE_FAST_FAILS = 5

# signal.SIGKILL does not exist on Windows. _SIGKILL is None there, and the
# default `sig` of kill_process_tree (None) means 'forceful kill' on every
# platform, so merely importing this module never raises AttributeError.
_SIGKILL = getattr(signal, 'SIGKILL', None)
_SIGTERM = getattr(signal, 'SIGTERM', signal.SIGINT)


def _enable_windows_ansi() -> None:
    """Enable ANSI escape sequences on Windows consoles."""
    if sys.platform == 'win32':
        # os.system('') enables virtual terminal processing in Windows cmd.exe
        os.system('')


def format_prefixed_line(service_name: str, color_code: str, line: str) -> str:
    """Format a single output line with an aligned, colored service prefix."""
    prefix = f'[{service_name}]'
    return f'{color_code}{prefix:<10}{RESET} | {line}'


def format_plain_line(service_name: str, line: str) -> str:
    """Format a single output line for the unified log file (aligned prefix, no colors)."""
    prefix = f'[{service_name}]'
    return f'{prefix:<10} | {line}'


@dataclass
class ServiceConfig:
    name: str
    cmd: list[str]
    cwd: Path
    color: str
    env: dict[str, str]


def build_service_configs(mode: str, root_dir: Path | None = None) -> list[ServiceConfig]:
    """Configure only this backend; ``root_dir`` always denotes the backend root.

    Keep ``mode`` for compatibility with existing dev/prod invocations. The bot
    uses the same entry point in both modes.
    """
    if mode not in ('dev', 'prod'):
        raise ValueError(f'Unknown run mode: {mode}')
    root = (root_dir or Path(__file__).resolve().parents[1]).resolve()
    bot_script = root / 'bot.py'
    if not bot_script.is_file():
        return []

    bot_env = dict(os.environ)
    # The runner writes the log file, so the bot must not duplicate it.
    bot_env['AYLA_RUNNER_MANAGED'] = '1'
    bot_env['PYTHONUNBUFFERED'] = '1'
    return [ServiceConfig('bot', [sys.executable, str(bot_script)], root, COLORS['bot'], bot_env)]


def kill_process_tree(pid: int, sig: signal.Signals | None = None) -> None:
    """Terminate a process and all of its spawned children.

    Defaults to a forceful kill (SIGKILL where available, ``taskkill /F /T``
    on Windows). Pass ``signal.SIGTERM`` to request graceful termination.
    The default is None rather than ``signal.SIGKILL`` because SIGKILL does
    not exist on Windows.
    """
    if sys.platform == 'win32':
        sigterm = getattr(signal, 'SIGTERM', None)
        force = sig is None or (sigterm is not None and sig != sigterm)
        try:
            flag = ['/F'] if force else []
            subprocess.run(
                ['taskkill', *flag, '/T', '/PID', str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass
    else:
        if sig is None:
            sig = _SIGKILL if _SIGKILL is not None else _SIGTERM
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        except Exception:
            pass

        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        except Exception:
            pass


class ProcessManager:
    """Manages concurrent child processes, log streaming, and synchronized shutdown."""

    def __init__(self, configs: list[ServiceConfig], log_path: Path | None = None) -> None:
        self.configs = configs
        self.processes: dict[str, subprocess.Popen[str]] = {}
        self.threads: list[threading.Thread] = []
        self._shutdown_event = threading.Event()
        self._start_times: dict[str, float] = {}
        self._fast_fails: dict[str, int] = {}
        self._log_lock = threading.Lock()
        self._log_file: TextIO | None = None
        if log_path is not None:
            try:
                self._log_file = open(log_path, 'a', encoding='utf-8', errors='replace')
            except OSError as exc:
                print(f'[runner] | [WARNING] Cannot write unified log to {log_path}: {exc}', file=sys.stderr)

    def _emit(self, name: str, color: str, line: str) -> None:
        """Print a prefixed line to the terminal and append it to the log file."""
        print(format_prefixed_line(name, color, line), flush=True)
        if self._log_file is None:
            return
        with self._log_lock:
            try:
                self._log_file.write(format_plain_line(name, line) + '\n')
                self._log_file.flush()
            except (ValueError, OSError):
                pass

    def _stream_output(self, name: str, color: str, pipe: TextIO | None) -> None:
        if pipe is None:
            return
        try:
            for raw_line in iter(pipe.readline, ''):
                if not raw_line:
                    break
                line = raw_line.rstrip('\r\n')
                self._emit(name, color, line)
        except (ValueError, OSError):
            pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    def start(self) -> None:
        """Start all configured services and start streaming their logs."""
        _enable_windows_ansi()

        for cfg in self.configs:
            self._emit(
                'runner',
                COLORS['runner'],
                f"Starting service '{cfg.name}' ({' '.join(cfg.cmd)})...",
            )
            self._launch_service(cfg)

            # Give a tiny breath between launching services
            time.sleep(0.5)

    def _launch_service(self, cfg: ServiceConfig) -> None:
        """Spawn one service process and attach its log-reader thread."""
        popen_kwargs: dict = {
            'cwd': str(cfg.cwd),
            'env': cfg.env,
            'stdout': subprocess.PIPE,
            'stderr': subprocess.STDOUT,
            'text': True,
            'bufsize': 1,
            'encoding': 'utf-8',
            'errors': 'replace',
        }

        if sys.platform == 'win32':
            popen_kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:

            def _preexec() -> None:
                os.setsid()
                try:
                    import ctypes

                    libc = ctypes.CDLL(None)
                    # PR_SET_PDEATHSIG = 1: delivers SIGKILL to child if parent dies
                    libc.prctl(1, signal.SIGKILL)
                except Exception:
                    pass

            popen_kwargs['preexec_fn'] = _preexec

        proc = subprocess.Popen(cfg.cmd, **popen_kwargs)
        self.processes[cfg.name] = proc
        self._start_times[cfg.name] = time.monotonic()

        reader_thread = threading.Thread(
            target=self._stream_output,
            args=(cfg.name, cfg.color, proc.stdout),
            daemon=True,
            name=f'{cfg.name}-output',
        )
        reader_thread.start()
        self.threads.append(reader_thread)

    def stop_all(self) -> None:
        """Stop all processes and terminate their child trees."""
        if self._shutdown_event.is_set():
            return
        self._shutdown_event.set()

        self._emit('runner', COLORS['runner'], 'Stopping all services...')

        # First pass: SIGTERM to allow graceful shutdown
        for proc in self.processes.values():
            if sys.platform != 'win32' or proc.poll() is None:
                kill_process_tree(proc.pid, signal.SIGTERM)

        # Wait briefly for termination
        deadline = time.monotonic() + 2.0
        for proc in self.processes.values():
            remaining = max(0.05, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                pass

        # Second pass: force-kill any remaining processes
        for proc in self.processes.values():
            if sys.platform != 'win32' or proc.poll() is None:
                kill_process_tree(proc.pid)

        self._emit('runner', COLORS['runner'], 'All services stopped.')

        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            finally:
                self._log_file = None

    def run_until_exit(self) -> int:
        """Monitor processes, restarting any that exit, until shutdown is requested."""

        def _handle_signal(signum: int, frame: object) -> None:
            self.stop_all()

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        if hasattr(signal, 'SIGHUP'):
            signal.signal(signal.SIGHUP, _handle_signal)

        try:
            while not self._shutdown_event.is_set():
                for cfg in self.configs:
                    proc = self.processes.get(cfg.name)
                    if proc is None:
                        continue
                    ret = proc.poll()
                    if ret is None or self._shutdown_event.is_set():
                        continue
                    if not self._restart_service(cfg, ret):
                        return ret or 1
                time.sleep(0.3)
        except KeyboardInterrupt:
            self.stop_all()
            return 0

        return 0

    def _restart_service(self, cfg: ServiceConfig, ret: int) -> bool:
        """Restart a service that exited. Returns False when giving up (fatal)."""
        old_proc = self.processes.get(cfg.name)
        if old_proc is not None and sys.platform != 'win32':
            kill_process_tree(old_proc.pid)

        lived = time.monotonic() - self._start_times.get(cfg.name, 0.0)
        if lived < FAST_FAIL_WINDOW_S:
            self._fast_fails[cfg.name] = self._fast_fails.get(cfg.name, 0) + 1
        else:
            self._fast_fails[cfg.name] = 0

        if self._fast_fails[cfg.name] > MAX_CONSECUTIVE_FAST_FAILS:
            self._emit(
                'runner',
                COLORS['runner'],
                f"Service '{cfg.name}' crashed {self._fast_fails[cfg.name]} times in a row. Shutting down...",
            )
            self.stop_all()
            return False

        self._emit(
            'runner',
            COLORS['runner'],
            f"Service '{cfg.name}' exited with code {ret}. Restarting in {RESTART_DELAY_S:g}s...",
        )
        if self._shutdown_event.wait(RESTART_DELAY_S):
            return True
        self._launch_service(cfg)
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Run only the Projeqt-Ayla backend with logs and automatic restarts.')
    parser.add_argument(
        '--mode',
        choices=['dev', 'prod'],
        default='dev',
        help='Compatibility option; both modes run only the backend bot.',
    )
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    configs = build_service_configs(args.mode, root_dir=root)
    if not configs:
        print(
            format_prefixed_line('runner', COLORS['runner'], '[ERROR] No services configured to run.'),
            file=sys.stderr,
        )
        return 1

    manager = ProcessManager(configs, log_path=resolve_dated_log_path(root))
    import atexit

    atexit.register(manager.stop_all)
    try:
        manager.start()
        return manager.run_until_exit()
    finally:
        manager.stop_all()


if __name__ == '__main__':
    sys.exit(main())
