"""A persistent root shell.

One `su` process stays open (spawning su per command costs 50-200 ms and floods Magisk logs). Every command is wrapped
so its exit code arrives on a line of its own after a unique marker. The leading newline fixes PandoraGuard's bug where
output without a trailing newline swallowed the marker and stalled until the watchdog fired.
"""
from __future__ import annotations

import logging
import os
import queue
import shlex
import shutil
import subprocess
import threading
import uuid
from typing import Protocol

log = logging.getLogger("rh.shell")

SU_CANDIDATES = ["su", "/system/bin/su", "/system/xbin/su", "/sbin/su", "/su/bin/su", "/debug_ramdisk/su",
                 "/data/adb/ksu/bin/su", "/data/adb/ap/bin/su"]


def q(s: str) -> str:
    """Quote one shell argument."""
    return shlex.quote(str(s))


class Shell(Protocol):
    def run(self, cmd: str, timeout: float = 30.0) -> tuple[int, str]: ...
    def close(self) -> None: ...


class ShellError(Exception):
    pass


def find_su() -> str | None:
    for c in SU_CANDIDATES:
        p = shutil.which(c) if "/" not in c else (c if os.path.exists(c) else None)
        if p:
            return p
    return None


class RootShell:
    def __init__(self, su_path: str | None = None, require_root: bool = True):
        self.su = su_path or find_su()
        if not self.su:
            raise ShellError("su not found: turn root on for this phone and allow Termux")
        self.require_root = require_root
        self._proc: subprocess.Popen[bytes] | None = None
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._lock = threading.Lock()

    def _start(self) -> None:
        self._lines = queue.Queue()
        self._proc = subprocess.Popen([self.su], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, bufsize=0)
        lines = self._lines
        out = self._proc.stdout

        def pump() -> None:
            assert out is not None
            for raw in iter(out.readline, b""):
                lines.put(raw)
            lines.put(None)  # EOF: the shell died

        threading.Thread(target=pump, name="su-reader", daemon=True).start()
        rc, outp = self._run_locked("id", 15.0)
        if self.require_root and "uid=0" not in outp:
            self._kill()
            raise ShellError(f"root was refused ({outp.strip()[:80] or 'no answer'})")

    def run(self, cmd: str, timeout: float = 30.0) -> tuple[int, str]:
        with self._lock:
            if not self._proc or self._proc.poll() is not None:
                self._start()
            return self._run_locked(cmd, timeout)

    def _run_locked(self, cmd: str, timeout: float) -> tuple[int, str]:
        assert self._proc and self._proc.stdin
        marker = f"__RH_EOF_{uuid.uuid4().hex[:12]}__"
        script = f"{{ {cmd}\n}} 2>&1 </dev/null; printf '\\n{marker}%s\\n' \"$?\"\n"
        try:
            self._proc.stdin.write(script.encode())
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            self._kill()
            raise ShellError(f"root shell closed: {e}") from None
        buf: list[str] = []
        while True:
            try:
                raw = self._lines.get(timeout=timeout)
            except queue.Empty:
                log.warning("command timed out after %.0fs, restarting root shell: %s", timeout, cmd[:120])
                self._kill()
                return 124, "".join(buf)
            if raw is None:
                self._kill()
                raise ShellError("root shell exited")
            line = raw.decode("utf-8", "replace")
            if line.startswith(marker):
                try:
                    rc = int(line[len(marker):].strip() or 0)
                except ValueError:
                    rc = 1
                out = "".join(buf)
                if out.endswith("\n"):
                    out = out[:-1]  # the newline we added before the marker
                return rc, out
            buf.append(line)

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if not proc:
            return
        try:
            proc.kill()
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for f in (proc.stdin, proc.stdout):
            try:
                if f:
                    f.close()
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            if self._proc and self._proc.stdin:
                try:
                    self._proc.stdin.write(b"exit\n")
                    self._proc.stdin.flush()
                except OSError:
                    pass
            self._kill()
