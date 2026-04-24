"""Codex CLI integration for web search extraction.

Uses RUST_LOG=trace to capture raw SSE websocket events from the OpenAI Responses
API, extracting native web_search_call items and annotations. Falls back to parsing
the JSONL output if trace data is unavailable.

Usage: python -m llm_search.providers.codex "your prompt" [-m model] [--raw-dir /tmp]
"""

import argparse
import json
import logging
import os
import re
import uuid
from datetime import datetime

import sh

from llm_search.config import CODEX_DEFAULT_MODEL, CODEX_DEFAULT_OUTPUT_DIR
from llm_search.prompts import load_system_prompt

logger = logging.getLogger(__name__)


def call_codex(prompt, model, timeout_seconds, trace_log_path):
    """Call Codex CLI in exec mode via sh with JSONL output and RUST_LOG=trace for raw SSE capture."""
    logger.debug("call_codex(model=%s, timeout=%ds, trace_log=%s)", model, timeout_seconds, trace_log_path)

    trace_environment = {**os.environ, "RUST_LOG": "codex_api=trace"}
    system_prompt = load_system_prompt()

    augmented_prompt = f'CRITICAL RULE-> using web_search answer: "{prompt}"'

    logger.info("Running: codex exec --json -m %s (with RUST_LOG=codex_api=trace) ...", model)
    with open(trace_log_path, "w") as trace_file:
        raw_output = sh.codex(
            "-c", "service_tier=fast",
            "exec",
            "--json",
            "-m", model,
            "--config", "model_reasoning_effort=medium",
            "--config", f"instructions={system_prompt}",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--ephemeral",
            augmented_prompt,
            _env=trace_environment,
            _ok_code=[0, 1],
            _encoding="utf-8",
            _err=trace_file,
            _timeout=timeout_seconds,
        )

    raw_text = str(raw_output)
    logger.debug("call_codex returned %d chars on stdout", len(raw_text))
    return raw_text


def parse_jsonl_events(raw_text):
    """Parse JSONL text into a list of event dicts."""
    return [
        json.loads(line.strip())
        for line in raw_text.splitlines()
        if line.strip() and line.strip().startswith("{")
    ]


def parse_trace_log_sse_events(trace_log_path):
    """Parse raw SSE websocket events from the RUST_LOG=trace stderr output.

    Opened with errors='replace' because codex 0.124.0+ writes ANSI escape
    sequences that include stray non-UTF-8 bytes; strict decoding would abort
    the whole parse on the first bad byte.
    """
    sse_events = []
    websocket_event_pattern = re.compile(r'websocket event: ({.*})\s*$')
    with open(trace_log_path, encoding="utf-8", errors="replace") as trace_file:
        for line in trace_file:
            match = websocket_event_pattern.search(line)
            if not match:
                continue
            try:
                sse_events.append(json.loads(match.group(1)))
            except json.JSONDecodeError:
                pass
    logger.debug("Parsed %d SSE events from trace log", len(sse_events))
    return sse_events


def extract_native_api_items(sse_events):
    """Extract web_search_call items and output text with annotations from raw SSE events."""
    web_search_calls = []
    output_text_items = []

    for event in sse_events:
        event_type = event.get("type", "")

        if event_type == "response.output_item.done":
            item = event.get("item", {})
            if item.get("type") == "web_search_call":
                web_search_calls.append(item)

        elif event_type == "response.completed":
            response = event.get("response", {})
            for item in response.get("output", []):
                if item.get("type") == "web_search_call":
                    web_search_calls.append(item)
                elif item.get("type") == "message":
                    for content_block in item.get("content", []):
                        if content_block.get("type") == "output_text":
                            output_text_items.append(content_block)

    return web_search_calls, output_text_items


def extract_search_queries_from_api(web_search_calls):
    """Extract search queries from native API web_search_call items."""
    queries = []
    for call in web_search_calls:
        action = call.get("action", {})
        if action.get("type") == "search":
            action_queries = action.get("queries", [])
            if action_queries:
                queries.extend(action_queries)
            elif action.get("query"):
                queries.append(action["query"])
    return queries


def extract_search_queries_from_jsonl(events):
    """Fallback: extract search queries from JSONL web_search items."""
    queries = []
    for event in events:
        if event.get("type") != "item.completed":
            continue
        item = event.get("item", {})
        if item.get("type") != "web_search":
            continue
        action = item.get("action", {})
        if action.get("type") == "search":
            action_queries = action.get("queries", [])
            if action_queries:
                queries.extend(action_queries)
            elif action.get("query"):
                queries.append(action["query"])
        elif item.get("query"):
            queries.append(item["query"])
    return queries


def extract_model_response(events):
    """Get the final agent_message text (last completed agent_message)."""
    last_message = ""
    for event in events:
        if event.get("type") != "item.completed":
            continue
        item = event.get("item", {})
        if item.get("type") == "agent_message" and item.get("text"):
            last_message = item["text"]
    return last_message


def extract_markdown_link_annotations(model_text):
    """Extract url_citation annotations from markdown links and bare URLs in the model response."""
    annotations = [
        {
            "type": "url_citation",
            "start_index": match.start(),
            "end_index": match.end(),
            "url": match.group(2),
            "title": match.group(1),
        }
        for match in re.finditer(r'\[([^\]]+)\]\(([^)]+)\)', model_text)
    ]

    linked_spans = {(annotation["start_index"], annotation["end_index"]) for annotation in annotations}
    for match in re.finditer(r'https?://[^\s)\]>]+', model_text):
        is_inside_markdown_link = any(
            start <= match.start() < end for start, end in linked_spans
        )
        if not is_inside_markdown_link:
            domain = re.sub(r'^https?://(www\.)?', '', match.group(0)).split('/')[0]
            annotations.append({
                "type": "url_citation",
                "start_index": match.start(),
                "end_index": match.end(),
                "url": match.group(0),
                "title": domain,
            })

    return annotations


def build_annotations(model_text, native_annotations):
    """Build url_citation annotations from native API annotations or markdown links."""
    if native_annotations:
        annotations = native_annotations
    else:
        annotations = extract_markdown_link_annotations(model_text)

    annotations = [annotation for annotation in annotations if annotation.get("url", "").startswith("http")]

    seen_keys = set()
    unique_annotations = [
        annotation for annotation in annotations
        if (key := (annotation["url"], annotation["start_index"], annotation["end_index"])) not in seen_keys
        and not seen_keys.add(key)
    ]

    return sorted(unique_annotations, key=lambda annotation: (annotation["start_index"], annotation["end_index"]))


def build_openai_format(search_queries, model_text, native_annotations):
    """Build OpenAI Responses API-style output from parsed Codex data."""
    output = []

    unique_queries = list(dict.fromkeys(search_queries))
    if unique_queries:
        output.append({
            "type": "web_search_call",
            "status": "completed",
            "action": {
                "type": "search",
                "queries": unique_queries,
            },
        })

    if model_text:
        annotations = build_annotations(model_text, native_annotations)
        output.append({
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": model_text,
                    "annotations": annotations,
                }
            ],
        })

    return output


def run_search(prompt, model, output_dir, timeout):
    """Run Codex web search and return OpenAI-format result.

    Args:
        prompt: The user's search query.
        model: Codex model name (e.g. "gpt-5.5").
        output_dir: Directory to save intermediate files.
        timeout: CLI timeout in seconds.

    Returns:
        Tuple of (openai_output_list, model_response_text).
    """
    logger.debug("run_search(model=%s, timeout=%d)", model, timeout)
    timestamp = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    raw_jsonl_path = os.path.join(output_dir, f"codex_raw_{timestamp}.jsonl")
    trace_log_path = os.path.join(output_dir, f"codex_trace_{timestamp}.log")
    search_json_path = os.path.join(output_dir, f"codex_search_{timestamp}.json")

    raw_text = call_codex(prompt, model, timeout, trace_log_path)
    events = parse_jsonl_events(raw_text)
    with open(raw_jsonl_path, "w") as output_file:
        json.dump(events, output_file, indent=2)

    sse_events = parse_trace_log_sse_events(trace_log_path)
    web_search_calls, output_text_items = extract_native_api_items(sse_events)

    native_annotations = []
    for output_text_item in output_text_items:
        native_annotations.extend(output_text_item.get("annotations", []))

    if web_search_calls:
        search_queries = extract_search_queries_from_api(web_search_calls)
    else:
        search_queries = extract_search_queries_from_jsonl(events)

    model_response = extract_model_response(events)
    openai_output = build_openai_format(search_queries, model_response, native_annotations)

    with open(search_json_path, "w") as output_file:
        json.dump(openai_output, output_file, indent=2)

    logger.debug("run_search returning %d chars response", len(model_response))
    return openai_output, model_response


def build_argument_parser():
    """Build CLI argument parser for standalone usage."""
    parser = argparse.ArgumentParser(description="Query Codex CLI and extract search citations.")
    parser.add_argument("prompt", help="The prompt to send to Codex")
    parser.add_argument("-m", "--model", default=CODEX_DEFAULT_MODEL)
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging")
    parser.add_argument("--timeout", type=int, default=120, help="Timeout in seconds")
    parser.add_argument("--raw-dir", default=CODEX_DEFAULT_OUTPUT_DIR, help="Directory for output files")
    return parser


def main():
    """CLI entry point for standalone Codex search."""
    from llm_search.logging_setup import setup_colorized_logging

    parser = build_argument_parser()
    args = parser.parse_args()
    setup_colorized_logging(verbose=args.verbose)

    timestamp = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    raw_jsonl_path = os.path.join(args.raw_dir, f"codex_raw_{timestamp}.jsonl")
    trace_log_path = os.path.join(args.raw_dir, f"codex_trace_{timestamp}.log")
    search_json_path = os.path.join(args.raw_dir, f"codex_search_{timestamp}.json")

    logger.info("Calling Codex model=%s", args.model)
    raw_text = call_codex(args.prompt, args.model, args.timeout, trace_log_path)

    events = parse_jsonl_events(raw_text)
    with open(raw_jsonl_path, "w") as output_file:
        json.dump(events, output_file, indent=2)
    logger.info("Raw JSONL saved to %s (%d events)", raw_jsonl_path, len(events))

    sse_events = parse_trace_log_sse_events(trace_log_path)
    web_search_calls, output_text_items = extract_native_api_items(sse_events)
    logger.info("Trace log: %d SSE events, %d web_search_calls, %d output_text items",
                len(sse_events), len(web_search_calls), len(output_text_items))

    native_annotations = []
    for output_text_item in output_text_items:
        native_annotations.extend(output_text_item.get("annotations", []))

    if web_search_calls:
        search_queries = extract_search_queries_from_api(web_search_calls)
        logger.info("Using native API web_search_call data (%d queries)", len(search_queries))
    else:
        search_queries = extract_search_queries_from_jsonl(events)
        logger.info("Falling back to JSONL web_search data (%d queries)", len(search_queries))

    model_response = extract_model_response(events)

    openai_output = build_openai_format(search_queries, model_response, native_annotations)
    with open(search_json_path, "w") as output_file:
        json.dump(openai_output, output_file, indent=2)
    logger.info("Search data saved to %s", search_json_path)
    logger.info("Trace log at %s", trace_log_path)

    print(model_response)


if __name__ == "__main__":
    main()
