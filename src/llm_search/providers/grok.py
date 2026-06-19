"""xAI Grok CLI integration for web search extraction.

Drives the Grok CLI (`grok`) in headless single-turn mode (`grok -p ... --output-format json`)
on the host's logged-in xAI account (browser/OAuth login stored in ~/.grok/auth.json — no API
key). Grok's agentic `web_search` tool grounds the answer; the shared grounding system prompt is
injected VERBATIM via `--system-prompt-override` (Grok's true system-prompt replacement, unlike
`--rules`, which only appends a `<human_rules>` block to the default prompt). The `--output-format
json` mode emits a single object `{text, stopReason, sessionId, requestId, thought}`; citations
arrive as inline markdown links in `text` (footnote style, e.g. `[[1]](https://…)`).

Tools are NOT restricted: a `--tools` allowlist breaks the `grok-build` agent (it requires
`run_terminal_cmd`), so we approve all tools (`--always-approve`) and rely on the research system
prompt + the container's non-root isolation — the same posture as the agy/kimi providers.

Usage: python -m llm_search.providers.grok "your prompt" [-m model] [--raw-dir /tmp]
"""

import argparse
import json
import logging
import os
import re
import uuid
from datetime import datetime
from urllib.parse import urlparse

import sh

from llm_search.config import (
    GROK_BINARY,
    GROK_DEFAULT_MODEL,
    GROK_DEFAULT_OUTPUT_DIR,
    GROK_SANDBOX_DIR,
    PROVIDER_DEFAULTS,
)
from llm_search.prompts import load_system_prompt
from llm_search.providers.subprocess_safety import build_sanitized_environment, kill_subprocess_tree_on_done

logger = logging.getLogger(__name__)

# Grok footnote citation: [[1]](url) and plain [title](url). The shared MARKDOWN_LINK_PATTERN
# can't match the doubled `[[`/`]]`, so this allows one optional inner bracket pair and a
# balanced-paren URL tail (Wikipedia/MDN round-trip).
GROK_CITATION_PATTERN = re.compile(
    r"\[\[?([^\]\[]+)\]?\]\((https?://[^()\s]+(?:\([^()]*\)[^()\s]*)?)\)"
)


def build_augmented_prompt(prompt):
    """Wrap the user query so the agent is pushed to ground its answer via web search."""
    return f'CRITICAL RULE-> using web_search answer: "{prompt}"'


def build_grok_args(augmented_prompt, system_prompt, model, sandbox_dir):
    """Assemble the grok argv for headless single-turn JSON mode.

    `--system-prompt-override` REPLACES the default agent prompt verbatim (the grounding prompt
    claude/codex also inject). `--cwd` MUST be absolute (a relative path errors with os error 2).
    A per-request `--leader-socket` isolates any leader process grok spawns so the `_done`
    pgroup-kill never reaps a leader serving a concurrent request. `--no-auto-update` keeps the
    container on its pinned grok version; `--no-memory`/`--no-subagents` keep the turn stateless.
    """
    return [
        "-p", augmented_prompt,
        "--output-format", "json",
        "--system-prompt-override", system_prompt,
        "--model", model,
        "--no-subagents", "--no-memory", "--no-auto-update",
        "--always-approve",
        "--leader-socket", os.path.join(sandbox_dir, "leader.sock"),
        "--cwd", sandbox_dir,
    ]


def call_grok(prompt, model, timeout_seconds, environment, sandbox_dir, stderr_path):
    """Call grok in headless JSON mode via sh; return raw stdout text. Stderr -> stderr_path.

    No PTY is needed (headless `-p` prints clean JSON to stdout and exits). `_ok_code=[0, 1]`
    tolerates grok's exit-1-on-error (the JSON error object or the stderr message is surfaced by
    detect_provider_failure). `_new_session` + `_done` reap the whole process tree (grok forks a
    Go-style leader/agent helper) the moment sh reaps the leader.
    """
    augmented_prompt = build_augmented_prompt(prompt)
    system_prompt = load_system_prompt()
    grok = sh.Command(GROK_BINARY)
    logger.info("Running: grok -p ... --model %s --output-format json (cwd=%s)", model, sandbox_dir)
    raw_output = grok(
        *build_grok_args(augmented_prompt, system_prompt, model, sandbox_dir),
        _env=environment,
        _err=stderr_path,
        _ok_code=[0, 1],
        _encoding="utf-8",
        _timeout=timeout_seconds,
        _new_session=True,
        _done=kill_subprocess_tree_on_done(),
    )
    raw_text = str(raw_output)
    logger.debug("call_grok returned %d chars on stdout", len(raw_text))
    return raw_text


def parse_grok_json(raw_text):
    """Parse grok's single JSON object from stdout. Returns {} if the payload isn't JSON.

    grok json mode emits exactly one object; with --no-auto-update there are no stray stdout
    log lines. Still tolerant: locate the outermost object if leading bytes slip in.
    """
    stripped = raw_text.strip()
    if not stripped:
        return {}
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(stripped[start:end + 1])
            except json.JSONDecodeError:
                return {}
        return {}


def read_stderr_tail(stderr_path, limit=300):
    """Return the last `limit` chars of the grok stderr file (diagnostics), or ''."""
    try:
        with open(stderr_path, encoding="utf-8", errors="replace") as stderr_file:
            return stderr_file.read()[-limit:]
    except OSError:
        return ""


def detect_provider_failure(data, model_response, stderr_tail):
    """Return an error message if the grok run failed, else None.

    grok signals failure two ways: a JSON error event `{"type":"error","message":...}` on
    stdout, or a non-JSON exit-1 (empty/garbage stdout) with the reason on stderr. Either way
    we raise so server.py returns HTTP 500 rather than HTTP 200 with empty content.
    """
    if data.get("type") == "error":
        return data.get("message") or "<grok error event with no message>"
    if not model_response:
        detail = stderr_tail.strip() or "<no stdout, no stderr>"
        return f"grok returned empty text ({detail})"
    return None


def citation_title(label, url):
    """Pick a human-ish citation title: the link label unless it's a bare footnote number."""
    cleaned = (label or "").strip()
    if cleaned and not cleaned.isdigit():
        return cleaned
    netloc = urlparse(url).netloc
    return netloc[4:] if netloc.startswith("www.") else netloc


def build_citation_annotations(model_text):
    """Build OpenAI url_citation annotations from the markdown links in the model text.

    Each annotation spans the whole `[[n]](url)` / `[title](url)` token; duplicate (url, span)
    pairs are dropped and the result is sorted by position.
    """
    annotations = []
    seen_keys = set()
    for match in GROK_CITATION_PATTERN.finditer(model_text):
        label, url = match.group(1), match.group(2)
        key = (url, match.start(), match.end())
        if key in seen_keys:
            continue
        seen_keys.add(key)
        annotations.append({
            "type": "url_citation",
            "start_index": match.start(),
            "end_index": match.end(),
            "url": url,
            "title": citation_title(label, url),
        })
    return sorted(annotations, key=lambda annotation: (annotation["start_index"], annotation["end_index"]))


def build_openai_format(model_text, annotations):
    """Build the OpenAI Responses-style output list (web_search_call + annotated message)."""
    output = []
    if annotations:
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
            "text": model_text,
            "annotations": annotations,
        }],
    })
    return output


def discard_request_sandbox(request_sandbox, parent_sandbox_dir):
    """Move a used per-request grok cwd into a date-stamped .trash subdir.

    grok runs with `--cwd`/`--leader-socket` inside this dir; trashing it after each request
    reaps the leader socket and any scratch files. Mirrors the gemini/kimi providers. Cleanup
    must never throw — it runs from a `finally:` where a raise would mask the real exception.
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
    """Run Grok web search via `grok -p` and return OpenAI-format result.

    Args:
        prompt: The user's search query.
        model: grok model id (e.g. "grok-build").
        output_dir: Directory to save intermediate files.
        timeout: CLI timeout in seconds.
        request_id: Per-request id used as the artefact filename suffix.
        environment: Process environment dict (defaults to a sanitized os.environ).
        sandbox_dir: Parent dir for the per-request grok cwd (defaults to GROK_SANDBOX_DIR).

    Returns:
        Tuple of (openai_output_list, model_response_text).
    """
    logger.debug("run_search(model=%s, timeout=%d, request_id=%s)", model, timeout, request_id)
    if request_id is None:
        request_id = uuid.uuid4().hex[:12]
    raw_path = os.path.join(output_dir, f"grok_raw_{request_id}.json")
    search_path = os.path.join(output_dir, f"grok_search_{request_id}.json")
    stderr_path = os.path.join(output_dir, f"grok_stderr_{request_id}.log")

    parent_sandbox = os.path.abspath(sandbox_dir if sandbox_dir is not None else GROK_SANDBOX_DIR)
    request_sandbox = os.path.join(parent_sandbox, request_id)
    os.makedirs(request_sandbox, mode=0o700, exist_ok=True)

    # grok authenticates via ~/.grok/auth.json; strip credential-shaped env vars (XAI_API_KEY,
    # other providers' keys) so a tool escape can't exfiltrate them from the gunicorn process.
    grok_environment = build_sanitized_environment(environment if environment is not None else os.environ)
    try:
        raw_text = call_grok(prompt, model, timeout, grok_environment, request_sandbox, stderr_path)
    finally:
        discard_request_sandbox(request_sandbox, parent_sandbox)

    data = parse_grok_json(raw_text)
    model_response = data.get("text", "") if isinstance(data, dict) else ""
    with open(raw_path, "w") as raw_file:
        json.dump({"parsed": data, "raw": raw_text}, raw_file, indent=2)

    failure_message = detect_provider_failure(data if isinstance(data, dict) else {}, model_response, read_stderr_tail(stderr_path))
    if failure_message:
        logger.error("Grok run failed: %s", failure_message[:240])
        raise RuntimeError(f"grok provider failed: {failure_message}")

    annotations = build_citation_annotations(model_response)
    openai_output = build_openai_format(model_response, annotations)
    with open(search_path, "w") as search_file:
        json.dump(openai_output, search_file, indent=2)

    logger.debug("run_search returning %d chars response, %d citations", len(model_response), len(annotations))
    return openai_output, model_response


def build_argument_parser():
    """Build CLI argument parser for standalone usage."""
    parser = argparse.ArgumentParser(description="Query the Grok CLI and extract search citations.")
    parser.add_argument("prompt", help="The prompt to send to grok")
    parser.add_argument("-m", "--model", default=GROK_DEFAULT_MODEL)
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging")
    parser.add_argument("--timeout", type=int, default=PROVIDER_DEFAULTS["grok"]["timeout"], help="Timeout in seconds")
    parser.add_argument("--raw-dir", default=GROK_DEFAULT_OUTPUT_DIR, help="Directory for output files")
    return parser


def main():
    """CLI entry point for standalone Grok search."""
    from llm_search.logging_setup import setup_colorized_logging

    parser = build_argument_parser()
    args = parser.parse_args()
    setup_colorized_logging(verbose=args.verbose)

    request_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    logger.info("Calling Grok model=%s", args.model)
    openai_output, model_response = run_search(
        args.prompt, args.model, args.raw_dir, args.timeout, request_id=request_id
    )
    logger.info("Got %d output items, %d response chars", len(openai_output), len(model_response))
    print(model_response)


if __name__ == "__main__":
    main()
