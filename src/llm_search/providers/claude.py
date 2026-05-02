"""Claude Code CLI integration for web search extraction.

Calls Claude Code's built-in WebSearch tool, parses stream-json output,
and extracts search queries and citations in OpenAI Responses API format.

Usage: python -m llm_search.providers.claude "your prompt" [-m model] [--raw-dir /tmp]
"""

import argparse
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime

import sh

from llm_search.config import CLAUDE_ALLOWED_TOOLS, CLAUDE_DEFAULT_MODEL, CLAUDE_DEFAULT_OUTPUT_DIR, PROVIDER_DEFAULTS
from llm_search.prompts import load_system_prompt
from llm_search.providers.subprocess_safety import kill_subprocess_tree_on_done
from llm_search.response import find_markdown_links

logger = logging.getLogger(__name__)


def build_claude_environment(parent_environment):
    """Strip CLAUDE_*/CLAUDECODE_* vars from a parent env mapping; force NODE_NO_WARNINGS=1."""
    clean = {
        key: value for key, value in parent_environment.items()
        if not key.startswith("CLAUDECODE") and not key.startswith("CLAUDE_CODE_")
    }
    clean["NODE_NO_WARNINGS"] = "1"
    return clean


def call_claude(prompt, model, output_dir, timeout_seconds, environment):
    """Call Claude Code CLI in print mode via sh, return raw stream-json text."""
    logger.debug("call_claude(model=%s, output_dir=%s, timeout=%ds)", model, output_dir, timeout_seconds)

    clean_environment = build_claude_environment(environment)
    system_prompt = load_system_prompt()
    augmented_prompt = f'CRITICAL RULE-> using web_search answer: "{prompt}"'

    logger.info("Running: claude -p ... --model %s ...", model)
    raw_output = sh.claude(
        "-p", augmented_prompt,
        "--model", model,
        "--output-format", "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--permission-mode", "bypassPermissions",
        "--system-prompt", system_prompt,
        "--tools", ",".join(CLAUDE_ALLOWED_TOOLS),
        "--mcp-config", '{"mcpServers":{}}',
        "--strict-mcp-config",
        _env=clean_environment,
        _ok_code=[0, 1],
        _encoding="utf-8",
        _timeout=timeout_seconds,
        _new_session=True,
        _done=kill_subprocess_tree_on_done(),
    )

    raw_text = str(raw_output)
    logger.info("call_claude returned %d chars", len(raw_text))
    return raw_text


def parse_stream_events(raw_text):
    """Parse raw stream-json text into a list of event dicts."""
    return [
        json.loads(line.strip())
        for line in raw_text.splitlines()
        if line.strip() and line.strip().startswith("{")
    ]


def is_search_tool(tool_name):
    """Check if a tool name is an allowed web search tool."""
    return tool_name in CLAUDE_ALLOWED_TOOLS


def extract_search_queries(stream_events):
    """Extract search queries from tool_use events for search tools."""
    queries = []
    for event in stream_events:
        if event.get("type") != "assistant":
            continue
        for content_block in event.get("message", {}).get("content", []):
            if content_block.get("type") != "tool_use":
                continue
            if not is_search_tool(content_block.get("name", "")):
                continue
            tool_input = content_block.get("input", {})
            query = tool_input.get("query", "")
            if query:
                queries.append(query)
    return queries


def parse_builtin_websearch_results(text):
    """Parse built-in WebSearch tool results (text with embedded 'Links: [...]' JSON)."""
    links_match = re.search(r"Links:\s*(\[.*?\])", text, re.DOTALL)
    if not links_match:
        return []
    try:
        links = json.loads(links_match.group(1))
    except json.JSONDecodeError:
        return []
    summary_text = text[links_match.end():].strip()
    return [
        {"url": link["url"], "title": link.get("title", ""), "content": summary_text}
        for link in links
        if link.get("url")
    ]


def extract_search_results(stream_events):
    """Extract search result sources from tool_result events that follow search tool calls."""
    search_tool_ids = set()
    for event in stream_events:
        if event.get("type") != "assistant":
            continue
        for content_block in event.get("message", {}).get("content", []):
            if content_block.get("type") == "tool_use" and is_search_tool(content_block.get("name", "")):
                search_tool_ids.add(content_block.get("id"))

    all_sources = []
    for event in stream_events:
        if event.get("type") != "user":
            continue
        for content_block in event.get("message", {}).get("content", []):
            if content_block.get("type") != "tool_result":
                continue
            if content_block.get("tool_use_id") not in search_tool_ids:
                continue
            result_content = content_block.get("content", [])
            if isinstance(result_content, str):
                result_content = [{"type": "text", "text": result_content}]
            for result_part in result_content:
                if result_part.get("type") != "text":
                    continue
                all_sources.extend(parse_builtin_websearch_results(result_part.get("text", "")))
    return all_sources


def extract_model_response(stream_events):
    """Get final model response text from assistant messages."""
    response_parts = []
    for event in stream_events:
        if event.get("type") != "assistant":
            continue
        for content_block in event.get("message", {}).get("content", []):
            if content_block.get("type") == "text":
                response_parts.append(content_block["text"])
    return "\n".join(response_parts) if response_parts else ""


def detect_provider_failure(stream_events, model_response):
    """Return an error message string if the Claude run failed, else None.

    Two failure modes:
    1. Explicit: a `result` event with `is_error: True` carries the failure
       text in the `result` field on auth failures, rate limits, and other
       CLI errors. Coerces non-string `result` values (dict/list) to JSON so
       the caller's `failure_message[:240]` slice doesn't crash with TypeError.
    2. Silent: stream ends with no is_error=True event AND no assistant text.
       This happens on truncated streams, kernel kills, transport drops, and
       upstream errors that close the stream without an error event. Without
       this guard the API returns HTTP 200 with empty `content` — a contract
       violation. Mirrors codex/gemini/kimi which already have this guard.
    """
    for event in stream_events:
        if event.get("type") != "result":
            continue
        if event.get("is_error"):
            return _ensure_failure_string(event.get("result"), "<unknown claude error>")
    if not model_response:
        return "claude returned empty model response (auth/rate-limit/CLI-crash likely)"
    return None


def _ensure_failure_string(value, fallback):
    """Coerce a polymorphic event field into a string for the failure-message channel."""
    if isinstance(value, str) and value:
        return value
    if value:
        try:
            return json.dumps(value, default=str)
        except (TypeError, ValueError):
            return str(value)
    return fallback


def extract_markdown_link_annotations(model_text, search_sources):
    """Extract url_citation annotations from markdown links [title](url) in the model response."""
    search_url_set = {source["url"] for source in search_sources}
    search_title_map = {source["url"]: source["title"] for source in search_sources}
    annotations = []

    for match, link_title, link_url in find_markdown_links(model_text):
        start_index = match.start()
        end_index = match.end()

        matched_url = link_url if link_url in search_url_set else next(
            (source_url for source_url in search_url_set if link_url in source_url or source_url in link_url),
            link_url,
        )
        annotations.append({
            "type": "url_citation",
            "start_index": start_index,
            "end_index": end_index,
            "url": matched_url,
            "title": search_title_map.get(matched_url, link_title),
        })

    return annotations


def extract_content_match_annotations(model_text, search_sources):
    """Fall back to matching source content sentences against the model response text."""
    annotations = []
    for source in search_sources:
        source_sentences = [
            sentence.strip()
            for sentence in re.split(r'(?<=[.!?])\s+', source.get("content", ""))
            if len(sentence.strip()) > 30
        ]
        for sentence in source_sentences:
            start_index = model_text.find(sentence)
            if start_index == -1:
                words = sentence.split()
                if len(words) >= 6:
                    partial_phrase = " ".join(words[:6])
                    start_index = model_text.find(partial_phrase)
                    if start_index != -1:
                        next_period = model_text.find(".", start_index)
                        next_newline = model_text.find("\n", start_index)
                        ends = [end for end in [next_period, next_newline] if end != -1]
                        end_index = min(ends) + 1 if ends else start_index + len(partial_phrase)
                    else:
                        continue
                else:
                    continue
            else:
                end_index = start_index + len(sentence)
            annotations.append({
                "type": "url_citation",
                "start_index": start_index,
                "end_index": end_index,
                "url": source["url"],
                "title": source["title"],
            })
    return annotations


def build_annotations(model_text, search_sources):
    """Build url_citation annotations from markdown links or content matching."""
    markdown_annotations = extract_markdown_link_annotations(model_text, search_sources)
    if markdown_annotations:
        annotations = markdown_annotations
    else:
        annotations = extract_content_match_annotations(model_text, search_sources)

    seen_keys = set()
    unique_annotations = [
        annotation for annotation in annotations
        if (key := (annotation["url"], annotation["start_index"], annotation["end_index"])) not in seen_keys
        and not seen_keys.add(key)
    ]

    return sorted(unique_annotations, key=lambda annotation: (annotation["start_index"], annotation["end_index"]))


def build_openai_format(search_queries, search_sources, model_text):
    """Build OpenAI Responses API-style output from parsed Claude Code data."""
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

    annotations = build_annotations(model_text, search_sources)
    if model_text:
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


def run_search(prompt, model, output_dir, timeout, request_id=None, environment=None):
    """Run Claude Code web search and return OpenAI-format result.

    Args:
        prompt: The user's search query.
        model: Claude model name (e.g. "haiku").
        output_dir: Directory to save intermediate JSON files.
        timeout: CLI timeout in seconds.
        request_id: Per-request id used as the artefact filename suffix (defaults to a fresh uuid hex).
        environment: Process environment dict to inherit (defaults to os.environ).
            Strips CLAUDECODE_*/CLAUDE_CODE_* keys and sets NODE_NO_WARNINGS=1.

    Returns:
        Tuple of (openai_output_list, model_response_text).
    """
    logger.debug("run_search(model=%s, timeout=%d, request_id=%s)", model, timeout, request_id)
    if request_id is None:
        request_id = uuid.uuid4().hex[:12]
    raw_json_path = os.path.join(output_dir, f"claude_raw_{request_id}.json")
    search_json_path = os.path.join(output_dir, f"claude_search_{request_id}.json")

    raw_text = call_claude(prompt, model, output_dir, timeout, environment if environment is not None else os.environ)
    stream_events = parse_stream_events(raw_text)
    with open(raw_json_path, "w") as output_file:
        json.dump(stream_events, output_file, indent=2)

    search_queries = extract_search_queries(stream_events)
    search_sources = extract_search_results(stream_events)
    model_response = extract_model_response(stream_events)

    failure_message = detect_provider_failure(stream_events, model_response)
    if failure_message:
        logger.error("Claude run failed: %s", failure_message[:240])
        raise RuntimeError(f"claude provider failed: {failure_message}")

    openai_output = build_openai_format(search_queries, search_sources, model_response)

    with open(search_json_path, "w") as output_file:
        json.dump(openai_output, output_file, indent=2)

    logger.debug("run_search returning %d chars response", len(model_response))
    return openai_output, model_response


def build_argument_parser():
    """Build CLI argument parser for standalone usage."""
    parser = argparse.ArgumentParser(description="Query Claude Code CLI and extract search citations.")
    parser.add_argument("prompt", help="The prompt to send to Claude Code")
    parser.add_argument("-m", "--model", default=CLAUDE_DEFAULT_MODEL)
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging")
    parser.add_argument("--timeout", type=int, default=PROVIDER_DEFAULTS["claude"]["timeout"], help="Timeout in seconds")
    parser.add_argument("--raw-dir", default=CLAUDE_DEFAULT_OUTPUT_DIR, help="Directory for output files")
    return parser


def main():
    """CLI entry point for standalone Claude Code search."""
    from llm_search.logging_setup import setup_colorized_logging

    parser = build_argument_parser()
    args = parser.parse_args()
    setup_colorized_logging(verbose=args.verbose)

    timestamp = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    raw_json_path = os.path.join(args.raw_dir, f"claude_raw_{timestamp}.json")
    search_json_path = os.path.join(args.raw_dir, f"claude_search_{timestamp}.json")

    logger.info("Calling Claude Code model=%s", args.model)
    raw_text = call_claude(args.prompt, args.model, args.raw_dir, args.timeout, os.environ)

    stream_events = parse_stream_events(raw_text)
    with open(raw_json_path, "w") as output_file:
        json.dump(stream_events, output_file, indent=2)
    logger.info("Raw stream-json saved to %s (%d events)", raw_json_path, len(stream_events))

    search_queries = extract_search_queries(stream_events)
    search_sources = extract_search_results(stream_events)
    model_response = extract_model_response(stream_events)
    logger.info("Found %d search queries, %d sources", len(search_queries), len(search_sources))

    failure_message = detect_provider_failure(stream_events, model_response)
    if failure_message:
        logger.error("Claude run failed: %s", failure_message[:240])
        sys.exit(2)

    openai_output = build_openai_format(search_queries, search_sources, model_response)
    with open(search_json_path, "w") as output_file:
        json.dump(openai_output, output_file, indent=2)
    logger.info("Search data saved to %s", search_json_path)

    print(model_response)


if __name__ == "__main__":
    main()
