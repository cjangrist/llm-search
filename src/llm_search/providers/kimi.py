"""Kimi CLI integration for web search extraction.

Calls Kimi (MoonshotAI) CLI in print mode with stream-json output. Subprocess +
file I/O lives here; the JSONL parsing / annotation building is in kimi_parsing.

Usage: python -m llm_search.providers.kimi "your prompt" [-m model] [--raw-dir /tmp]
"""

import argparse
import json
import logging
import os
import uuid
from datetime import datetime

import sh

from llm_search.config import KIMI_DEFAULT_MODEL, KIMI_DEFAULT_OUTPUT_DIR, KIMI_SANDBOX_DIR, PROVIDER_DEFAULTS
from llm_search.prompts import load_system_prompt
from llm_search.providers.kimi_parsing import (
    build_openai_format,
    detect_provider_failure,
    extract_model_response,
    extract_search_queries,
    extract_search_sources,
    parse_stream_events,
)
from llm_search.providers.subprocess_safety import kill_subprocess_tree, make_done_callback

logger = logging.getLogger(__name__)

API_KEY_CONFIG_TEMPLATE = '''default_model = "direct/kimi-for-coding"

[models."direct/kimi-for-coding"]
provider = "direct-api"
model = "kimi-for-coding"
max_context_size = 262144
capabilities = ["thinking"]

[providers.direct-api]
type = "kimi"
base_url = "https://api.kimi.com/coding/v1"
api_key = "{api_key}"

[services.moonshot_search]
base_url = "https://api.kimi.com/coding/v1/search"
api_key = "{api_key}"

[services.moonshot_fetch]
base_url = "https://api.kimi.com/coding/v1/fetch"
api_key = "{api_key}"
'''


def build_kimi_arguments(model, augmented_prompt, sandbox_dir, config_file_path):
    """Assemble the kimi CLI argv, optionally pointing at a per-request --config-file."""
    kimi_arguments = [
        "--print", "--no-thinking", "--verbose",
        "--output-format", "stream-json",
        "-w", sandbox_dir,
        "-p", augmented_prompt,
    ]
    if model:
        kimi_arguments = ["-m", model, *kimi_arguments]
    if config_file_path:
        kimi_arguments = ["--config-file", config_file_path, *kimi_arguments]
    return kimi_arguments


def write_kimi_config_file(api_key, output_dir, request_id):
    """Render the api-key TOML to a per-request file with mode 0o600. Returns the path or None.

    Uses str.replace (not .format) so api keys containing literal `{` or `}` don't blow up.
    """
    if not api_key:
        return None
    config_path = os.path.join(output_dir, f"kimi_apiconfig_{request_id}.toml")
    rendered = API_KEY_CONFIG_TEMPLATE.replace("{api_key}", api_key)
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as config_file:
            config_file.write(rendered)
    except Exception:
        os.close(fd) if not config_file.closed else None  # type: ignore[name-defined]
        raise
    logger.info("write_kimi_config_file: wrote %s (mode 0o600, %d chars)", config_path, len(rendered))
    return config_path


def discard_kimi_config_file(config_path, output_dir):
    """Move a used kimi config file to a request-scoped trash subdir (RULE_07: no rm)."""
    if not config_path or not os.path.isfile(config_path):
        return
    trash_dir = os.path.join(output_dir, ".trash", datetime.now().strftime("%Y%m%d"))
    os.makedirs(trash_dir, mode=0o700, exist_ok=True)
    trashed = os.path.join(trash_dir, os.path.basename(config_path))
    try:
        os.replace(config_path, trashed)
        logger.info("discard_kimi_config_file: moved %s -> %s", config_path, trashed)
    except OSError as move_error:
        logger.warning("discard_kimi_config_file: failed to move %s: %s", config_path, move_error)


REDACT_VALUE_FLAGS = {"-p", "--prompt", "-c", "--command", "--config", "--config-file"}


def make_request_sandbox(parent_sandbox_dir, request_id):
    """Create a per-request subdir under parent_sandbox_dir with mode 0o700."""
    request_sandbox = os.path.join(parent_sandbox_dir, request_id)
    os.makedirs(request_sandbox, mode=0o700, exist_ok=True)
    return request_sandbox


def discard_request_sandbox(request_sandbox, parent_sandbox_dir):
    """Move a used request sandbox into a date-stamped .trash subdir (no rm; RULE_07)."""
    if not request_sandbox or not os.path.isdir(request_sandbox):
        return
    trash_dir = os.path.join(parent_sandbox_dir, ".trash", datetime.now().strftime("%Y%m%d"))
    os.makedirs(trash_dir, mode=0o700, exist_ok=True)
    trashed = os.path.join(trash_dir, os.path.basename(request_sandbox))
    try:
        os.replace(request_sandbox, trashed)
    except OSError as move_error:
        logger.warning("discard_request_sandbox: failed to move %s: %s", request_sandbox, move_error)


def redact_argv_for_logging(kimi_arguments):
    """Replace values of -p / -c / --config with `<redacted>` for safe logging (api keys + prompts)."""
    redacted = []
    skip_next = False
    for argument in kimi_arguments:
        if skip_next:
            redacted.append("<redacted>")
            skip_next = False
            continue
        redacted.append(argument)
        if argument in REDACT_VALUE_FLAGS:
            skip_next = True
    return redacted


def call_kimi(prompt, model, timeout_seconds, stderr_log_path, sandbox_dir, api_key, environment, output_dir, request_id):
    """Call Kimi CLI in print mode via sh with stream-json output and captured stderr.

    The api-key TOML is written to a per-request --config-file (mode 0600) instead of
    being passed inline as the value of --config — keeps the secret out of /proc/<pid>/cmdline,
    ps captures, and any other process that can read kernel argv state.
    """
    logger.info("call_kimi(model=%s, timeout=%ds, stderr_log=%s)", model or "(config default)", timeout_seconds, stderr_log_path)
    system_prompt = load_system_prompt()
    augmented_prompt = (
        f"{system_prompt}\n\n---\n\n"
        f'CRITICAL RULE-> using web_search answer: "{prompt}"'
    )
    logger.info("call_kimi: system_prompt=%d chars, user_prompt=%d chars, augmented=%d chars",
                len(system_prompt), len(prompt), len(augmented_prompt))

    os.makedirs(sandbox_dir, exist_ok=True)
    config_file_path = write_kimi_config_file(api_key, output_dir, request_id)
    if config_file_path:
        logger.info("call_kimi: api_key supplied — using --config-file %s", config_file_path)
    else:
        logger.info("call_kimi: no api_key supplied, falling back to OAuth config")

    kimi_arguments = build_kimi_arguments(model, augmented_prompt, sandbox_dir, config_file_path)
    logger.info("Running: kimi %s (prompt=%d chars)", " ".join(redact_argv_for_logging(kimi_arguments)), len(augmented_prompt))

    stderr_file = open(stderr_log_path, "w") if stderr_log_path else None
    captured_pid = [None]
    try:
        raw_output = sh.kimi(
            *kimi_arguments,
            _env=dict(environment), _ok_code=[0, 1], _encoding="utf-8",
            _err=stderr_file, _timeout=timeout_seconds,
            _new_session=True, _done=make_done_callback(captured_pid),
        )
    finally:
        if stderr_file is not None:
            stderr_file.close()
        kill_subprocess_tree(captured_pid[0])
        discard_kimi_config_file(config_file_path, output_dir)

    raw_text = str(raw_output)
    stderr_size = os.path.getsize(stderr_log_path) if stderr_log_path and os.path.isfile(stderr_log_path) else 0
    logger.info("call_kimi: stdout=%d chars stderr_log=%s stderr_bytes=%d", len(raw_text), stderr_log_path, stderr_size)
    return raw_text


def run_search(prompt, model, output_dir, timeout, request_id=None, environment=None, sandbox_dir=None, api_key=None):
    """Run Kimi web search and return OpenAI-format result.

    Args:
        prompt: The user's search query.
        model: Kimi model id (e.g. "kimi-code/kimi-for-coding"), or empty for config default.
        output_dir: Directory to save intermediate files.
        timeout: CLI timeout in seconds.
        request_id: Per-request id used as the artefact filename suffix (defaults to a fresh uuid hex).
        environment: Process environment dict (defaults to os.environ).
        sandbox_dir: CWD for the kimi CLI subprocess (defaults to KIMI_SANDBOX_DIR config).
        api_key: Kimi API key (defaults to KIMI_API_KEY env var). Empty string falls back to OAuth.

    Returns:
        Tuple of (openai_output_list, model_response_text).
    """
    logger.info("run_search(model=%s, timeout=%d, output_dir=%s, request_id=%s)", model or "(config default)", timeout, output_dir, request_id)
    if request_id is None:
        request_id = uuid.uuid4().hex[:12]
    raw_jsonl_path = os.path.join(output_dir, f"kimi_raw_{request_id}.json")
    search_json_path = os.path.join(output_dir, f"kimi_search_{request_id}.json")
    stderr_log_path = os.path.join(output_dir, f"kimi_stderr_{request_id}.log")

    resolved_environment = environment if environment is not None else os.environ
    parent_sandbox = sandbox_dir if sandbox_dir is not None else KIMI_SANDBOX_DIR
    resolved_api_key = api_key if api_key is not None else os.getenv("KIMI_API_KEY", "").strip()
    request_sandbox = make_request_sandbox(parent_sandbox, request_id)
    try:
        raw_text = call_kimi(prompt, model, timeout, stderr_log_path,
                             request_sandbox, resolved_api_key, resolved_environment,
                             output_dir, request_id)
    finally:
        discard_request_sandbox(request_sandbox, parent_sandbox)
    stream_events = parse_stream_events(raw_text)
    with open(raw_jsonl_path, "w") as output_file:
        json.dump(stream_events, output_file, indent=2)
    logger.info("run_search: wrote raw events -> %s (%d events)", raw_jsonl_path, len(stream_events))

    search_queries = extract_search_queries(stream_events)
    search_sources = extract_search_sources(stream_events)
    model_response = extract_model_response(stream_events)

    failure_message = detect_provider_failure(stream_events, model_response)
    if failure_message:
        logger.error("Kimi run failed: %s", failure_message[:240])
        raise RuntimeError(f"kimi provider failed: {failure_message}")

    openai_output = build_openai_format(search_queries, search_sources, model_response)

    annotation_count = sum(
        len(content.get("annotations", []))
        for item in openai_output if item.get("type") == "message"
        for content in item.get("content", [])
    )
    with open(search_json_path, "w") as output_file:
        json.dump(openai_output, output_file, indent=2)
    logger.info("run_search: wrote search json -> %s (queries=%d sources=%d response_chars=%d annotations=%d)",
                search_json_path, len(search_queries), len(search_sources), len(model_response), annotation_count)
    return openai_output, model_response


def build_argument_parser():
    """Build CLI argument parser for standalone usage."""
    parser = argparse.ArgumentParser(description="Query Kimi CLI and extract search citations.")
    parser.add_argument("prompt", help="The prompt to send to Kimi")
    parser.add_argument("-m", "--model", default=KIMI_DEFAULT_MODEL)
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging")
    parser.add_argument("--timeout", type=int, default=PROVIDER_DEFAULTS["kimi"]["timeout"], help="Timeout in seconds")
    parser.add_argument("--raw-dir", default=KIMI_DEFAULT_OUTPUT_DIR, help="Directory for output files")
    return parser


def main():
    """CLI entry point for standalone Kimi search."""
    from llm_search.logging_setup import setup_colorized_logging

    parser = build_argument_parser()
    args = parser.parse_args()
    setup_colorized_logging(verbose=args.verbose)

    request_id = uuid.uuid4().hex[:12]
    raw_jsonl_path = os.path.join(args.raw_dir, f"kimi_raw_{request_id}.json")
    search_json_path = os.path.join(args.raw_dir, f"kimi_search_{request_id}.json")

    logger.info("Calling Kimi model=%s request_id=%s", args.model or "(config default)", request_id)
    stderr_log_path = os.path.join(args.raw_dir, f"kimi_stderr_{request_id}.log")
    raw_text = call_kimi(
        args.prompt, args.model, args.timeout, stderr_log_path,
        KIMI_SANDBOX_DIR, os.getenv("KIMI_API_KEY", "").strip(), os.environ,
        args.raw_dir, request_id,
    )

    stream_events = parse_stream_events(raw_text)
    with open(raw_jsonl_path, "w") as output_file:
        json.dump(stream_events, output_file, indent=2)
    logger.info("Raw stream-json saved to %s (%d events)", raw_jsonl_path, len(stream_events))

    search_queries = extract_search_queries(stream_events)
    search_sources = extract_search_sources(stream_events)
    model_response = extract_model_response(stream_events)
    logger.info("Found %d search queries, %d sources", len(search_queries), len(search_sources))

    openai_output = build_openai_format(search_queries, search_sources, model_response)
    with open(search_json_path, "w") as output_file:
        json.dump(openai_output, output_file, indent=2)
    logger.info("Search data saved to %s", search_json_path)

    print(model_response)


if __name__ == "__main__":
    main()
