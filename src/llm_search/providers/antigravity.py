"""Google Antigravity CLI (`agy`) integration for web search extraction.

Drives the Antigravity CLI in non-interactive print mode (`agy --print`) using the
host's logged-in Google account OAuth (Google AI Pro/Ultra subscription) — `agy`
has no API-key auth path (upstream issue #78), so it always authenticates via the
stored OAuth credentials under ~/.gemini/antigravity-cli/.

Three CLI quirks drive the implementation:
  1. `agy --print` gates its stdout emission on isatty() — piped/redirected stdout
     yields zero bytes (issues #76/#408/#318). We run it under a pseudo-terminal
     (sh `_tty_out=True`) so the gate passes, exactly like the gemini provider.
  2. There is no structured/JSON output mode, so citations are parsed from the
     rendered markdown answer, where sources appear as `[title](redirect_url)` with
     Vertex grounding-redirect URLs — the same format gemini.py already resolves.
  3. agy has no system-prompt flag (upstream issue #50 is open) and ignores
     GEMINI_SYSTEM_MD, so we cannot inject the shared grounding prompt the way claude
     (`--system-prompt`) and codex (`--config instructions=`) do. The only headless
     channel agy honors is a "Rules" context file in its workspace Customizations Root
     (`.agents/AGENTS.md`, relative to the cwd); agy concatenates it into the system
     prompt of every request (host-verified 2026-06-19). We write load_system_prompt()
     there so all four providers share one grounding prompt. agy APPENDS this to its
     baked coding-assistant persona (it cannot be replaced), so the file leads with a
     header that re-scopes agy from "build web apps" to search-grounded research.

Usage: python -m llm_search.providers.antigravity "your prompt" [-m model] [--raw-dir /tmp]
"""

import argparse
import json
import logging
import os
import re
import signal
import sys
import uuid
from datetime import datetime

import sh

from llm_search.config import (
    ANTIGRAVITY_BINARY,
    ANTIGRAVITY_DEFAULT_MODEL,
    ANTIGRAVITY_DEFAULT_OUTPUT_DIR,
    ANTIGRAVITY_SANDBOX_DIR,
    GEMINI_DEFAULT_MODEL,
    PROVIDER_DEFAULTS,
)
from llm_search.providers.vertex_redirect import (
    VERTEX_REDIRECT_PATTERN,
    replace_inline_redirects_in_text,
    resolve_all_uris,
)
from llm_search.providers.subprocess_safety import (
    build_sanitized_environment,
    kill_subprocess_tree_on_done,
)
from llm_search.prompts import load_system_prompt

logger = logging.getLogger(__name__)

# Control sequences the PTY render leaves in the captured stream: CSI (colors/cursor),
# OSC (window-title) sequences agy emits, plus bare CR/NUL/BEL.
ANSI_PATTERN = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|[\r\x00\x07]"
)
# Markdown inline link: [visible title](url). Used to lift citations out of the answer.
MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
# Persistent failure markers agy prints to stdout (transient pre-auth ones go to --log-file).
FAILURE_MARKERS = (
    "you are not logged into antigravity",
    "individual quota reached",
    "quota exceeded",
    "authentication failed",
    "please sign in",
)


def strip_ansi(text):
    """Remove ANSI/OSC/control sequences the pseudo-terminal render injects."""
    return ANSI_PATTERN.sub("", text)


def build_augmented_prompt(prompt):
    """Wrap the user query so the agent is pushed to ground its answer via web search."""
    return (
        "Use web search to answer the following and cite every claim with its source "
        f'URL in markdown link form. Question: "{prompt}"'
    )


# agy's workspace "Rules" file (relative to the cwd Customizations Root). agy concatenates
# this into every request's system prompt. AGENTS.md under `.agents/` is the path agy's own
# baked system prompt documents as the Workspace Customizations Root (host-verified).
AGENT_RULES_RELATIVE_PATH = os.path.join(".agents", "AGENTS.md")
# Leads the injected grounding prompt: agy's baked persona is a web-app coding assistant, and
# the rules only APPEND (cannot replace), so we explicitly re-scope it to research first.
AGENT_RULES_HEADER = (
    "# Operating Mode for This Workspace (MANDATORY — overrides your defaults)\n\n"
    "You are NOT building software, web apps, or artifacts in this workspace. For every "
    "request here you operate strictly as the search-grounded research assistant defined "
    "below. These rules take precedence over your default coding-assistant behavior, "
    "design/aesthetics guidance, and artifact conventions: do not scaffold projects, write "
    "code files, or create artifacts — directly answer the user's question, grounded in web "
    "search, exactly as specified.\n\n"
    "---\n\n"
)


def write_agent_rules_file(workspace_dir):
    """Inject the shared grounding system prompt into agy via its workspace Rules file.

    This is agy's analogue of claude's `--system-prompt` and codex's `--config instructions=`
    — all three inject the same load_system_prompt() so the four providers share one grounding
    prompt. agy exposes no flag for it, so we use the `.agents/AGENTS.md` context file it does
    honor. Returns the path written.
    """
    rules_path = os.path.join(workspace_dir, AGENT_RULES_RELATIVE_PATH)
    os.makedirs(os.path.dirname(rules_path), mode=0o700, exist_ok=True)
    with open(rules_path, "w") as rules_file:
        rules_file.write(AGENT_RULES_HEADER + load_system_prompt() + "\n")
    logger.debug("Wrote agy grounding rules file: %s", rules_path)
    return rules_path


def build_agy_args(augmented_prompt, model, log_file_path, print_timeout_seconds):
    """Assemble the full agy argv. The prompt is the VALUE of `--print`, and `--print`
    MUST come first: agy uses Go's flag parser, which stops at the first bare positional —
    a prompt placed before the flags would drop every flag and fall back to interactive
    (bubbletea) mode. `--dangerously-skip-permissions` auto-approves the web-search tool
    call; `--log-file` diverts the glog server log off the PTY so stdout is just the answer.
    `--model` is omitted when no model is requested (agy uses its configured default).
    """
    args = [
        "--print", augmented_prompt,
        "--dangerously-skip-permissions",
        "--print-timeout", f"{print_timeout_seconds}s",
        "--log-file", log_file_path,
    ]
    if model:
        args += ["--model", model]
    return args


def call_agy(prompt, model, output_dir, timeout_seconds, environment, sandbox_dir, request_id):
    """Run `agy --print` under a PTY and return (cleaned_text, raw_text, log_file_path).

    The external sh `_timeout` is the real guard — agy's own `--print-timeout` is documented
    as unreliable (issue #76). `_tty_out=True` allocates the pseudo-terminal that both defeats
    the isatty() stdout gate AND gives agy a controlling terminal (its bubbletea TUI opens
    /dev/tty directly — so we must NOT add `_new_session`/setsid, which would detach it). The
    `_done` callback killpg's the child's own process group (safe: its pgid is the child pid,
    never the parent's) to reap any helper processes agy leaves behind.
    """
    log_file_path = os.path.join(output_dir, f"antigravity_run_{request_id}.log")
    augmented_prompt = build_augmented_prompt(prompt)
    print_timeout = max(15, timeout_seconds - 10)
    agy = sh.Command(ANTIGRAVITY_BINARY)
    logger.debug("call_agy(model=%s, timeout=%ds, request_id=%s)", model or "<default>", timeout_seconds, request_id)
    raw_output = agy(
        *build_agy_args(augmented_prompt, model, log_file_path, print_timeout),
        _env=environment, _cwd=sandbox_dir,
        _tty_out=True, _tty_size=(50, 220),
        _timeout=timeout_seconds, _timeout_signal=signal.SIGTERM,
        _ok_code=list(range(0, 130)),
        _done=kill_subprocess_tree_on_done(),
    )
    raw_text = str(raw_output)
    cleaned_text = strip_ansi(raw_text).strip()
    logger.debug("call_agy returned %d cleaned chars (raw %d)", len(cleaned_text), len(raw_text))
    return cleaned_text, raw_text, log_file_path


def detect_provider_failure(cleaned_text):
    """Return an error message if the agy run failed, else None.

    Empty stdout is the isatty-gate / silent-hang failure mode; the FAILURE_MARKERS catch
    auth/quota errors agy prints in lieu of an answer. Either way we surface HTTP 500 rather
    than an HTTP 200 with empty content.
    """
    if not cleaned_text:
        return "agy returned empty output (isatty stdout gate, auth failure, or CLI hang)"
    lowered = cleaned_text.lower()
    marker = next((m for m in FAILURE_MARKERS if m in lowered), None)
    if marker and len(cleaned_text) < 400:
        return f"agy reported failure: {cleaned_text[:200]}"
    return None


def collect_redirect_uris(text):
    """Union of Vertex grounding-redirect URLs found in markdown links and inline prose."""
    link_uris = [url for _title, url in MARKDOWN_LINK_PATTERN.findall(text)]
    inline_uris = VERTEX_REDIRECT_PATTERN.findall(text)
    return list({uri for uri in link_uris + inline_uris if uri})


def build_citation_annotations(final_text):
    """Build OpenAI url_citation annotations from the markdown links in the resolved text.

    Offsets index `final_text` (post-resolution), so the caller must resolve redirects and
    substitute them into the text BEFORE calling this. Each link's visible-title span becomes
    the citation segment; duplicates (same url+span) are dropped.
    """
    annotations = []
    seen_keys = set()
    for match in MARKDOWN_LINK_PATTERN.finditer(final_text):
        title, url = match.group(1), match.group(2)
        start_index = match.start(1)
        end_index = match.end(1)
        key = (url, start_index, end_index)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        annotations.append({
            "type": "url_citation",
            "start_index": start_index,
            "end_index": end_index,
            "url": url,
            "title": title,
        })
    return sorted(annotations, key=lambda annotation: (annotation["start_index"], annotation["end_index"]))


def build_openai_format(final_text, has_search):
    """Build the OpenAI Responses-style output list (web_search_call + annotated message)."""
    output = []
    if has_search:
        output.append({
            "type": "web_search_call",
            "status": "completed",
            "action": {"type": "search", "queries": []},
        })
    output.append({
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{
            "type": "output_text",
            "text": final_text,
            "annotations": build_citation_annotations(final_text),
        }],
    })
    return output


def discard_request_sandbox(request_sandbox, parent_sandbox_dir):
    """Move a used per-request agy cwd into a date-stamped .trash subdir.

    agy runs with its cwd set to this sandbox; without cleanup each request leaks a dir
    (plus any scratchpad files agy drops, gh #133) under the parent. Mirrors the gemini/kimi
    providers. Cleanup must never throw — it runs from a `finally:` where an unexpected raise
    would mask the original provider exception.
    """
    try:
        if not request_sandbox or not os.path.isdir(request_sandbox):
            return
        trash_dir = os.path.join(parent_sandbox_dir, ".trash", datetime.now().strftime("%Y%m%d"))
        os.makedirs(trash_dir, mode=0o700, exist_ok=True)
        os.replace(request_sandbox, os.path.join(trash_dir, os.path.basename(request_sandbox)))
    except OSError as cleanup_error:
        logger.warning("discard_request_sandbox: cleanup failed for %s: %s", request_sandbox, cleanup_error)


def run_search(prompt, model, output_dir, timeout, request_id=None, environment=None, sandbox_dir=None):
    """Run Antigravity web search via `agy --print` and return OpenAI-format result.

    Args:
        prompt: The user's search query.
        model: agy model id (e.g. "gemini-3-pro"); "" uses agy's configured default.
        output_dir: Directory to save intermediate files.
        timeout: External CLI timeout in seconds (the real guard over --print-timeout).
        request_id: Per-request id used as the artefact filename suffix.
        environment: Process environment dict (defaults to a sanitized os.environ).
        sandbox_dir: CWD for the agy subprocess (defaults to ANTIGRAVITY_SANDBOX_DIR).

    Returns:
        Tuple of (openai_output_list, model_response_text).
    """
    logger.debug("run_search(model=%s, timeout=%d, request_id=%s)", model or "<default>", timeout, request_id)
    if request_id is None:
        request_id = uuid.uuid4().hex[:12]
    raw_path = os.path.join(output_dir, f"antigravity_raw_{request_id}.json")
    search_path = os.path.join(output_dir, f"antigravity_search_{request_id}.json")

    parent_sandbox = sandbox_dir if sandbox_dir is not None else ANTIGRAVITY_SANDBOX_DIR
    request_sandbox = os.path.join(parent_sandbox, request_id)
    os.makedirs(request_sandbox, mode=0o700, exist_ok=True)
    write_agent_rules_file(request_sandbox)

    # agy is OAuth-only; strip credential-shaped env vars so a tool-escape inside the agent
    # cannot exfiltrate the other providers' keys from the shared gunicorn process.
    agy_environment = build_sanitized_environment(environment if environment is not None else os.environ)
    try:
        cleaned_text, raw_text, _log_path = call_agy(
            prompt, model, output_dir, timeout, agy_environment, request_sandbox, request_id
        )
    finally:
        discard_request_sandbox(request_sandbox, parent_sandbox)
    with open(raw_path, "w") as raw_file:
        json.dump({"raw": raw_text, "cleaned": cleaned_text}, raw_file, indent=2)

    failure_message = detect_provider_failure(cleaned_text)
    if failure_message:
        logger.error("Antigravity run failed: %s", failure_message[:240])
        raise RuntimeError(f"antigravity provider failed: {failure_message}")

    resolution_map = resolve_all_uris(collect_redirect_uris(cleaned_text))
    final_text = replace_inline_redirects_in_text(cleaned_text, resolution_map)
    has_search = bool(MARKDOWN_LINK_PATTERN.search(final_text))
    openai_output = build_openai_format(final_text, has_search)
    with open(search_path, "w") as search_file:
        json.dump(openai_output, search_file, indent=2)

    logger.debug("run_search returning %d chars response", len(final_text))
    return openai_output, final_text


def run_search_as_gemini(prompt, model, output_dir, timeout, request_id=None, environment=None, sandbox_dir=None):
    """Serve the legacy `gemini/*` API through agy, pinned to a Gemini model alias.

    The standalone gemini CLI was retired for consumer Google accounts (gemini-cli now
    returns IneligibleTierError — Google migrated individuals to the Antigravity suite), so
    the `gemini` provider drives the same agy backend as `antigravity`, pinned to
    GEMINI_DEFAULT_MODEL (default "Gemini 3.5 Flash (Low)" — low thinking). The inbound
    gemini-cli model string (e.g. "search-fast") is ignored: agy doesn't recognize those
    aliases, and pinning here is what keeps existing `gemini/*` clients working unchanged.
    """
    del model
    return run_search(
        prompt, GEMINI_DEFAULT_MODEL, output_dir, timeout,
        request_id=request_id, environment=environment, sandbox_dir=sandbox_dir,
    )


def build_argument_parser():
    """Build CLI argument parser for standalone usage."""
    parser = argparse.ArgumentParser(description="Query Antigravity CLI (agy) and extract search citations.")
    parser.add_argument("prompt", help="The prompt to send to agy")
    parser.add_argument("-m", "--model", default=ANTIGRAVITY_DEFAULT_MODEL, help="agy model id ('' = agy default)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging")
    parser.add_argument("--timeout", type=int, default=PROVIDER_DEFAULTS["gemini"]["timeout"], help="Timeout in seconds")
    parser.add_argument("--raw-dir", default=ANTIGRAVITY_DEFAULT_OUTPUT_DIR, help="Directory for output files")
    return parser


def main():
    """CLI entry point for standalone Antigravity search."""
    from llm_search.logging_setup import setup_colorized_logging

    parser = build_argument_parser()
    args = parser.parse_args()
    setup_colorized_logging(verbose=args.verbose)

    request_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    logger.info("Calling Antigravity model=%s", args.model or "<default>")
    openai_output, model_response = run_search(
        args.prompt, args.model, args.raw_dir, args.timeout, request_id=request_id
    )
    logger.info("Got %d output items, %d response chars", len(openai_output), len(model_response))
    print(model_response)


if __name__ == "__main__":
    main()
