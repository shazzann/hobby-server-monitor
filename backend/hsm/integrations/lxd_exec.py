"""One-command-at-a-time execution inside a container, with hard bounds.

Pure orchestration: the caller supplies ``execute(argv, identity, on_stdout,
on_stderr) -> exit_code``, which in production is pylxd ``Instance.execute``
(see ``lxd.LxdAdapter.exec_command``) and in tests a fake. Nothing here ever
starts a host process or shell.

Bounds and why they are enforced the way they are
-------------------------------------------------
* Fixed argv ``timeout -s KILL <deadline> /bin/sh -c <command>``: the user's
  text is exactly one argv element of a *container-side* shell. Busybox and
  coreutils ``timeout`` both accept this form.
* pylxd ``execute`` blocks in ``wait_for_operation`` with no deadline of its
  own, and an HTTP timeout would not prove the guest process stopped. So it runs
  in a daemon thread and *we* decide when to give up, using our own monotonic
  clock (exit codes such as 137 are not trusted to mean "timed out").
* Output handlers count bytes and stop storing after the combined cap, but keep
  draining so the guest never blocks on a full pipe.
* Non-root commands run as uid/gid 1500 (``hsm``). After every command, and
  when a command overruns, we run ``kill -KILL -1`` *as uid 1500*: kill(-1)
  signals every process that uid may signal, i.e. all of the guest account's
  processes including ones the command backgrounded. This is what makes the
  terminal "one command at a time": nothing a user command starts outlives it.
  It is safe because the API admits only one active operation per container.
* Root commands rely on the ``timeout`` wrapper (we cannot ``kill -1`` as root
  without killing the container's init and services). A root command that
  backgrounds a process holding stdout open can therefore keep the exec alive;
  if the thread has not returned after the grace period the outcome is
  reported as ``unknown`` -- never retried.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from .lxd_errors import LxdRejected

GUEST_UID = 1500
GUEST_NAME = "hsm"
GUEST_HOME = "/home/hsm"
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

OVERRUN_SECONDS = 3.0      # wait this long past the in-container deadline
KILL_GRACE_SECONDS = 5.0   # bound for the kill exec and for the final wait
CLEANUP_ARGV = ["/bin/sh", "-c", "kill -KILL -1"]


@dataclass(frozen=True)
class Identity:
    uid: int
    gid: int
    cwd: str
    label: str                       # 'root' | 'hsm' -- reported in the result

    def environment(self) -> dict[str, str]:
        """Fixed, minimal environment. Never derived from the host's."""
        home = "/root" if self.uid == 0 else GUEST_HOME
        return {"PATH": SAFE_PATH, "HOME": home, "LANG": "C.UTF-8", "TERM": "dumb", "USER": self.label}


ROOT = Identity(0, 0, "/root", "root")
GUEST = Identity(GUEST_UID, GUEST_UID, GUEST_HOME, GUEST_NAME)
GUEST_CLEANUP = Identity(GUEST_UID, GUEST_UID, "/", GUEST_NAME)   # cwd '/' works even if home is missing

Handler = Callable[[bytes], None]
ExecuteFn = Callable[[list, Identity, Handler, Handler], int]


def command_argv(command: str, deadline_seconds: int) -> list[str]:
    return ["timeout", "-s", "KILL", str(int(deadline_seconds)), "/bin/sh", "-c", command]


class OutputSink:
    """Combined byte cap across stdout and stderr; thread-safe because ws4py
    delivers each stream from its own thread."""

    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self._lock = threading.Lock()
        self._bufs = {"stdout": bytearray(), "stderr": bytearray()}
        self.truncated = {"stdout": False, "stderr": False}
        self.total = 0

    def _take(self, stream: str, data) -> None:
        if not data:
            return
        if isinstance(data, str):            # defensive: decode=False should give bytes
            data = data.encode("utf-8", "replace")
        with self._lock:
            room = self.limit - self.total
            if room > 0:
                chunk = bytes(data[:room])
                self._bufs[stream].extend(chunk)
                self.total += len(chunk)
            if len(data) > max(room, 0):
                self.truncated[stream] = True   # keep draining, stop storing

    def stdout(self, data) -> None:
        self._take("stdout", data)

    def stderr(self, data) -> None:
        self._take("stderr", data)

    def text(self, stream: str) -> str:
        with self._lock:
            return bytes(self._bufs[stream]).decode("utf-8", errors="replace")


def _drain(_data) -> None:
    pass


def _run_in_thread(fn: Callable[[], int], name: str) -> tuple[threading.Thread, dict]:
    box: dict = {}

    def target() -> None:
        try:
            box["exit"] = fn()
        except BaseException as exc:          # reported, never raised from the thread
            box["error"] = exc
        finally:
            box["end"] = time.monotonic()

    t = threading.Thread(target=target, name=name, daemon=True)
    t.start()
    return t, box


def cleanup_guest(execute: ExecuteFn, *, grace: float = KILL_GRACE_SECONDS) -> bool:
    """Kill every process of uid 1500. Returns True if the kill exec returned
    in time (its exit code is irrelevant: ESRCH just means nothing was left)."""
    t, box = _run_in_thread(lambda: execute(list(CLEANUP_ARGV), GUEST_CLEANUP, _drain, _drain), "hsm-exec-kill")
    t.join(grace)
    return not t.is_alive() and "error" not in box


def run_command(execute: ExecuteFn, command: str, *, as_root: bool, deadline_seconds: int,
                max_output_bytes: int, overrun: float = OVERRUN_SECONDS,
                grace: float = KILL_GRACE_SECONDS) -> dict:
    """Run one command and return the contract's exec result dict.

    Raises ``LxdRejected`` only when LXD refused to start the command (nothing
    ran). Every other failure is reported as ``outcome='unknown'``.
    """
    ident = ROOT if as_root else GUEST
    sink = OutputSink(max_output_bytes)
    argv = command_argv(command, deadline_seconds)
    start = time.monotonic()
    t, box = _run_in_thread(lambda: execute(argv, ident, sink.stdout, sink.stderr), "hsm-exec")
    t.join(deadline_seconds + overrun)
    if t.is_alive():
        if not as_root:
            cleanup_guest(execute, grace=grace)
        t.join(grace)
    elif not as_root and not isinstance(box.get("error"), LxdRejected):
        # Normal completion: still reap anything the command backgrounded.
        cleanup_guest(execute, grace=grace)

    if not t.is_alive() and isinstance(box.get("error"), LxdRejected):
        raise box["error"]

    finished = not t.is_alive() and "error" not in box
    end = box.get("end") if finished else time.monotonic()
    elapsed = max(0.0, (end or time.monotonic()) - start)
    if not finished:
        outcome, exit_code = "unknown", None
    elif elapsed >= deadline_seconds:
        outcome, exit_code = "timed_out", box.get("exit")
    else:
        outcome, exit_code = "completed", box.get("exit")
    return {
        "outcome": outcome,
        "exit_code": exit_code,
        "stdout": sink.text("stdout"),
        "stderr": sink.text("stderr"),
        "stdout_truncated": sink.truncated["stdout"],
        "stderr_truncated": sink.truncated["stderr"],
        "duration_ms": int(elapsed * 1000),
        "user": ident.label,
    }
