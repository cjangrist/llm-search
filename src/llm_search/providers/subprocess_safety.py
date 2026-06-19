"""Subprocess-tree cleanup helpers shared across providers.

The `sh` library's `_timeout` only kills the immediate child. CLIs like kimi-cli and agy
(Antigravity, which forks a Go language-server) spawn helpers that become orphaned when the
parent dies — they keep holding credential files, keep emitting trace output, and (in the
worst case) keep making upstream API calls. Pass `_done=kill_subprocess_tree_on_done()` so the
whole pgroup dies the instant sh reaps the leader (no PID-reuse race window between reap and a
finally-block kill). Most providers also set `_new_session=True` so the child is a session
leader; the antigravity provider is the exception — it runs agy under a `_tty_out` PTY whose
controlling terminal `_new_session` would detach, breaking agy's TUI.
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


SECRET_NAME_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASSWD", "_PRIVATE_KEY")
SECRET_NAME_PREFIXES = ("ANTHROPIC_", "OPENAI_", "GOOGLE_", "GEMINI_", "VERTEX_", "MOONSHOT_", "KIMI_", "DOPPLER_", "AWS_", "AZURE_", "GCP_")


def _looks_like_secret(env_name):
    """Heuristic: is this env-var name plausibly a credential?"""
    if env_name.endswith(SECRET_NAME_SUFFIXES):
        return True
    if env_name.startswith(SECRET_NAME_PREFIXES):
        # Allow well-known non-secret config and FILE-PATH env vars (the file at the path may
        # be a secret, but the path string is not — it has to flow through to the CLI so it
        # can read the file). Examples: GEMINI_DEFAULT_MODEL, GOOGLE_APPLICATION_CREDENTIALS,
        # KIMI_SANDBOX_DIR, AWS_SHARED_CREDENTIALS_FILE.
        non_secret_substrings = (
            "_DIR", "_PATH", "_MODEL", "_HOST", "_PORT", "_REGION", "_PROJECT",
            "_BUCKET", "_TIMEOUT", "_DEFAULT", "_FILE", "_CERT", "_CREDENTIALS",
            "_PROFILE", "_URL", "_ENDPOINT",
        )
        return not any(substring in env_name for substring in non_secret_substrings)
    return False


def build_sanitized_environment(parent_environment, allow_names=()):
    """Strip credential-shaped env vars from a parent env mapping.

    Each provider CLI runs untrusted upstream content (web pages fetched by the model's
    web_search tool) and may itself execute child processes. We do not want a prompt-or-tool
    escape from any provider to walk off with the kimi/codex/anthropic API keys for the OTHER
    providers. The host-level secret transports (Anthropic OAuth ~/.claude, codex OAuth
    ~/.codex, gcloud-via-vertex, kimi `--config-file`) are intact; this only removes the
    redundant env-var copies that the CLI never reads.

    Args:
        parent_environment: source env mapping (typically os.environ).
        allow_names: iterable of literal env-var names to keep even if they look like secrets
            (use sparingly — only when a CLI MUST receive a key via env and there is no
            file/fd alternative).

    Returns:
        dict — a fresh dict (never the parent), suitable for `_env=` to sh.*().
    """
    allow_set = frozenset(allow_names)
    return {
        env_name: env_value
        for env_name, env_value in parent_environment.items()
        if env_name in allow_set or not _looks_like_secret(env_name)
    }
