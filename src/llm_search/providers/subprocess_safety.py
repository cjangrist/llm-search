"""Subprocess-tree cleanup helpers shared across providers.

The `sh` library's `_timeout` only kills the immediate child. CLIs like bun, gemini-cli, and
kimi-cli spawn helpers (node, python, etc.) which become orphaned when the parent dies — they
keep holding credential files, keep emitting trace output, and (in the worst case) keep making
upstream API calls. Wrap every sh.* invocation with `_new_session=True` so the immediate child
is a session leader, and pass `_done=kill_subprocess_tree_on_done()` so the whole pgroup dies
the instant sh reaps the leader (no PID-reuse race window between reap and a finally-block kill).
"""

import logging
import os
import signal

logger = logging.getLogger(__name__)


def killpg_silent(pid):
    """SIGKILL the process group identified by `pid`. Idempotent; swallows benign errors."""
    if not pid:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
        logger.debug("killpg_silent: SIGKILL pgroup %s", pid)
    except ProcessLookupError:
        pass
    except PermissionError as kill_error:
        logger.warning("killpg_silent: cannot kill pgroup %s — %s", pid, kill_error)


def kill_subprocess_tree_on_done():
    """Return an sh `_done` callback that SIGKILLs the immediate-child's process group.

    Captures `cmd.process.pid` and calls killpg the moment sh fires _done — i.e. immediately
    after waitpid() returns inside sh. This is strictly earlier than reading the pid after
    sh.cli(...) returns: it shrinks the PID-reuse race window from "between waitpid and our
    finally block" (potentially ms under load) to "single Python statement after waitpid"
    (microseconds). Surviving descendants of the original session/pgroup are caught.

    Use pattern:
        sh.cli(*args, _new_session=True, _done=kill_subprocess_tree_on_done(), _timeout=N, ...)
    """
    def callback(cmd, _success, _exit_code):
        try:
            pid = cmd.process.pid
        except AttributeError:
            return
        killpg_silent(pid)
    return callback
