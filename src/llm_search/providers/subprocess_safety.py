"""Subprocess-tree cleanup helpers shared across providers.

The `sh` library's `_timeout` only kills the immediate child. CLIs like bun, gemini-cli, and
kimi-cli spawn helpers (node, python, etc.) which become orphaned when the parent dies — they
keep holding credential files, keep emitting trace output, and (in the worst case) keep making
upstream API calls. Wrap every sh.* invocation with `_new_session=True` so the immediate child
is a session leader, and use this module's `kill_subprocess_tree` helper from the call site's
`_done` callback so the entire group dies even if `sh._timeout` already SIGKILLed the leader.
"""

import logging
import os
import signal

logger = logging.getLogger(__name__)


def kill_subprocess_tree(pid):
    """Send SIGKILL to the process group identified by `pid`.

    `pid` should be the leader of a session/pgroup created via `_new_session=True`. Safe to call
    even after the leader has already exited — surviving group members get caught.
    Idempotent: ProcessLookupError / PermissionError on already-reaped/escaped pids is swallowed.
    """
    if not pid:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
        logger.debug("kill_subprocess_tree: SIGKILL pgroup %s", pid)
    except ProcessLookupError:
        pass
    except PermissionError as kill_error:
        logger.warning("kill_subprocess_tree: cannot kill pgroup %s — %s", pid, kill_error)


def make_done_callback(pid_holder):
    """Return an sh `_done` callback that records the immediate-child PID into `pid_holder[0]`.

    Use pattern:
        captured = [None]
        try:
            raw = sh.cli(*args, _new_session=True, _done=make_done_callback(captured), _timeout=N, ...)
        finally:
            kill_subprocess_tree(captured[0])
    """
    def record_then_complete(cmd, _success, _exit_code):
        try:
            pid_holder[0] = cmd.process.pid
        except AttributeError:
            pid_holder[0] = None
    return record_then_complete
