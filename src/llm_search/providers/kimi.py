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
    extract_model_response,
    extract_search_queries,
    extract_search_sources,
    parse_stream_events,
)

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


def build_kimi_arguments(model, augmented_prompt, sandbox_dir):
    """Assemble the kimi CLI argv, including the api-key config override when KIMI_API_KEY is set."""
    kimi_arguments = [
        "--print", "--no-thinking", "--verbose",
        "--output-format", "stream-json",
        "-w", sandbox_dir,
        "-p", augmented_prompt,
    ]
    if model:
        kimi_arguments = ["-m", model, *kimi_arguments]

    kimi_api_key = os.getenv("KIMI_API_KEY", "").strip()
    if kimi_api_key:
        logger.info("call_kimi: using KIMI_API_KEY env (%d chars) via inline --config override", len(kimi_api_key))
        config_override = API_KEY_CONFIG_TEMPLATE.format(api_key=kimi_api_key)
        kimi_arguments = ["--config", config_override, *kimi_arguments]
    else:
        logger.info("call_kimi: no KIMI_API_KEY env set, falling back to OAuth config")
    return kimi_arguments


REDACT_VALUE_FLAGS = {"-p", "--prompt", "-c", "--command", "--config"}


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


def call_kimi(prompt, model, timeout_seconds, stderr_log_path=None):
    """Call Kimi CLI in print mode via sh with stream-json output and captured stderr."""
    logger.info("call_kimi(model=%s, timeout=%ds, stderr_log=%s)", model or "(config default)", timeout_seconds, stderr_log_path)
    system_prompt = load_system_prompt()
    augmented_prompt = (
        f"{system_prompt}\n\n---\n\n"
        f'CRITICAL RULE-> using web_search answer: "{prompt}"'
    )
    logger.info("call_kimi: system_prompt=%d chars, user_prompt=%d chars, augmented=%d chars",
                len(system_prompt), len(prompt), len(augmented_prompt))

    sandbox_dir = KIMI_SANDBOX_DIR
    os.makedirs(sandbox_dir, exist_ok=True)
    kimi_arguments = build_kimi_arguments(model, augmented_prompt, sandbox_dir)
    logger.info("Running: kimi %s (prompt=%d chars)", " ".join(redact_argv_for_logging(kimi_arguments)), len(augmented_prompt))

    stderr_file = open(stderr_log_path, "w") if stderr_log_path else None
    try:
        raw_output = sh.kimi(
            *kimi_arguments,
            _env={**os.environ}, _ok_code=[0, 1], _encoding="utf-8",
            _err=stderr_file, _timeout=timeout_seconds,
        )
    finally:
        if stderr_file is not None:
            stderr_file.close()

    raw_text = str(raw_output)
    stderr_size = os.path.getsize(stderr_log_path) if stderr_log_path and os.path.isfile(stderr_log_path) else 0
    logger.info("call_kimi: stdout=%d chars stderr_log=%s stderr_bytes=%d", len(raw_text), stderr_log_path, stderr_size)
    return raw_text


def run_search(prompt, model, output_dir, timeout):
    """Run Kimi web search and return OpenAI-format result.

    Args:
        prompt: The user's search query.
        model: Kimi model id (e.g. "kimi-code/kimi-for-coding"), or empty for config default.
        output_dir: Directory to save intermediate files.
        timeout: CLI timeout in seconds.

    Returns:
        Tuple of (openai_output_list, model_response_text).
    """
    logger.info("run_search(model=%s, timeout=%d, output_dir=%s)", model or "(config default)", timeout, output_dir)
    timestamp = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    raw_jsonl_path = os.path.join(output_dir, f"kimi_raw_{timestamp}.json")
    search_json_path = os.path.join(output_dir, f"kimi_search_{timestamp}.json")
    stderr_log_path = os.path.join(output_dir, f"kimi_stderr_{timestamp}.log")

    raw_text = call_kimi(prompt, model, timeout, stderr_log_path)
    stream_events = parse_stream_events(raw_text)
    with open(raw_jsonl_path, "w") as output_file:
        json.dump(stream_events, output_file, indent=2)
    logger.info("run_search: wrote raw events -> %s (%d events)", raw_jsonl_path, len(stream_events))

    search_queries = extract_search_queries(stream_events)
    search_sources = extract_search_sources(stream_events)
    model_response = extract_model_response(stream_events)
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

    timestamp = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    raw_jsonl_path = os.path.join(args.raw_dir, f"kimi_raw_{timestamp}.json")
    search_json_path = os.path.join(args.raw_dir, f"kimi_search_{timestamp}.json")

    logger.info("Calling Kimi model=%s", args.model or "(config default)")
    raw_text = call_kimi(args.prompt, args.model, args.timeout)

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
