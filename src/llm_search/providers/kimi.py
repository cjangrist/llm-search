"""Kimi CLI integration for web search extraction.

Calls Kimi (MoonshotAI) CLI in print mode with stream-json output, parses the
JSONL events for SearchWeb tool calls + tool results, and extracts the final
assistant text plus url_citation annotations in OpenAI Responses API format.

Usage: python -m llm_search.providers.kimi "your prompt" [-m model] [--raw-dir /tmp]
"""

import argparse
import json
import logging
import os
import re
from datetime import datetime

import sh

from llm_search.config import KIMI_DEFAULT_MODEL, KIMI_DEFAULT_OUTPUT_DIR, KIMI_SANDBOX_DIR
from llm_search.prompts import load_system_prompt

logger = logging.getLogger(__name__)

SEARCH_TOOL_NAMES = {"SearchWeb"}
FETCH_TOOL_NAMES = {"FetchURL"}
GROUNDING_TOOL_NAMES = SEARCH_TOOL_NAMES | FETCH_TOOL_NAMES
FETCHURL_SYSTEM_META_PATTERN = re.compile(r"<system>.*?</system>\s*", re.DOTALL)

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

    kimi_arguments = [
        "--print",
        "--no-thinking",
        "--verbose",
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

    redacted_args = []
    skip_next = False
    for argument in kimi_arguments:
        if skip_next:
            redacted_args.append("<redacted>")
            skip_next = False
            continue
        redacted_args.append(argument)
        if argument in {"-p", "--prompt", "-c", "--command", "--config"}:
            skip_next = True
    logger.info("Running: kimi %s (prompt=%d chars)", " ".join(redacted_args), len(augmented_prompt))

    stderr_file = open(stderr_log_path, "w") if stderr_log_path else None
    try:
        raw_output = sh.kimi(
            *kimi_arguments,
            _env={**os.environ},
            _ok_code=[0, 1],
            _encoding="utf-8",
            _err=stderr_file,
            _timeout=timeout_seconds,
        )
    finally:
        if stderr_file is not None:
            stderr_file.close()

    raw_text = str(raw_output)
    logger.info("call_kimi: stdout=%d chars%s", len(raw_text),
                (f", stderr_log=%s (%d bytes)" % (stderr_log_path, os.path.getsize(stderr_log_path))) if stderr_log_path and os.path.isfile(stderr_log_path) else "")
    return raw_text


def parse_stream_events(raw_text):
    """Parse raw stream-json text into a list of event dicts, skipping non-JSON footer lines."""
    events = []
    skipped_non_json = 0
    bad_json = 0
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("{"):
            skipped_non_json += 1
            logger.debug("parse_stream_events skipping non-JSON line: %r", stripped[:160])
            continue
        try:
            events.append(json.loads(stripped))
        except json.JSONDecodeError as decode_error:
            bad_json += 1
            logger.warning("parse_stream_events malformed JSONL line (%s): %r", decode_error, stripped[:200])
    logger.info("parse_stream_events: %d events parsed, %d non-JSON lines skipped, %d malformed JSON",
                len(events), skipped_non_json, bad_json)
    role_counts = {}
    for event in events:
        role = event.get("role", "?")
        role_counts[role] = role_counts.get(role, 0) + 1
    logger.info("parse_stream_events role breakdown: %s", role_counts)
    return events


def extract_search_queries(stream_events):
    """Collect SearchWeb tool call queries from assistant messages.

    Also logs every tool call name encountered so we can see non-search tools
    kimi invokes (FetchURL, Task, etc.) that we're currently ignoring.
    """
    queries = []
    tool_name_counts = {}
    for event in stream_events:
        if event.get("role") != "assistant":
            continue
        for tool_call in event.get("tool_calls") or []:
            function_spec = tool_call.get("function", {})
            tool_name = function_spec.get("name", "?")
            tool_name_counts[tool_name] = tool_name_counts.get(tool_name, 0) + 1
            if tool_name not in SEARCH_TOOL_NAMES:
                continue
            arguments_string = function_spec.get("arguments", "") or ""
            try:
                arguments = json.loads(arguments_string)
            except json.JSONDecodeError:
                logger.warning("extract_search_queries malformed arguments JSON: %r", arguments_string[:200])
                continue
            query = arguments.get("query") or arguments.get("q")
            if query:
                queries.append(query)
    logger.info("extract_search_queries: tool_calls_by_name=%s, search_queries_captured=%d",
                tool_name_counts, len(queries))
    return queries


def find_search_tool_call_ids(stream_events):
    """Collect tool_call ids whose function.name is a known search tool."""
    search_ids = set()
    for event in stream_events:
        if event.get("role") != "assistant":
            continue
        for tool_call in event.get("tool_calls") or []:
            if tool_call.get("function", {}).get("name") in SEARCH_TOOL_NAMES:
                if tool_call.get("id"):
                    search_ids.add(tool_call["id"])
    return search_ids


def find_fetchurl_tool_call_urls(stream_events):
    """Map tool_call id -> URL for every FetchURL call the model made.

    Kimi's FetchURL tool takes {"url": "..."} as arguments and returns the fetched
    page content as a list of text parts. Those responses don't have Title/URL/Summary
    metadata inline the way SearchWeb responses do, so we need to pair each response
    back to the URL the model asked to fetch.
    """
    url_map = {}
    for event in stream_events:
        if event.get("role") != "assistant":
            continue
        for tool_call in event.get("tool_calls") or []:
            function_spec = tool_call.get("function", {})
            if function_spec.get("name") not in FETCH_TOOL_NAMES:
                continue
            call_id = tool_call.get("id")
            if not call_id:
                continue
            try:
                arguments = json.loads(function_spec.get("arguments", "") or "")
            except json.JSONDecodeError:
                logger.warning("find_fetchurl_tool_call_urls malformed arguments: %r", function_spec.get("arguments", "")[:200])
                continue
            url = arguments.get("url")
            if url:
                url_map[call_id] = url
    return url_map


def flatten_tool_content(content):
    """Join list-of-parts tool content into a single string, stripping the <system> meta block."""
    if isinstance(content, list):
        text_parts = [part.get("text", "") for part in content if isinstance(part, dict)]
        content = "\n".join(text_parts)
    if not isinstance(content, str):
        return ""
    return FETCHURL_SYSTEM_META_PATTERN.sub("", content).strip()


def fetchurl_source_title(url, content_text):
    """Pick a reasonable title for a fetched page: first markdown heading if present, else domain."""
    for line in content_text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line.lstrip("#").strip()[:200]
    return re.sub(r"^https?://(www\.)?", "", url).split("/")[0]


def parse_search_result_text(tool_content_text):
    """Parse a SearchWeb result body into a list of {url, title, summary} entries."""
    entries = []
    for block in re.split(r"\n---+\n", tool_content_text):
        block = block.strip()
        if not block:
            continue
        title_match = re.search(r"^Title:\s*(.+?)\s*$", block, re.MULTILINE)
        url_match = re.search(r"^URL:\s*(\S+)\s*$", block, re.MULTILINE)
        summary_match = re.search(r"^Summary:\s*(.+)", block, re.MULTILINE | re.DOTALL)
        if not url_match:
            continue
        entries.append({
            "url": url_match.group(1).strip(),
            "title": title_match.group(1).strip() if title_match else "",
            "content": summary_match.group(1).strip() if summary_match else "",
        })
    return entries


def extract_search_sources(stream_events):
    """Collect grounding sources from tool messages matching SearchWeb and FetchURL calls."""
    search_tool_call_ids = find_search_tool_call_ids(stream_events)
    fetchurl_url_map = find_fetchurl_tool_call_urls(stream_events)
    logger.info("extract_search_sources: %d SearchWeb ids, %d FetchURL ids", len(search_tool_call_ids), len(fetchurl_url_map))
    sources = []
    tool_messages_total = 0
    search_matched = 0
    fetch_matched = 0
    for event in stream_events:
        if event.get("role") != "tool":
            continue
        tool_messages_total += 1
        tool_call_id = event.get("tool_call_id")

        if tool_call_id in search_tool_call_ids:
            search_matched += 1
            content = flatten_tool_content(event.get("content", ""))
            if content:
                parsed = parse_search_result_text(content)
                logger.info("extract_search_sources[SearchWeb]: id=%s content=%d chars parsed=%d sources",
                            tool_call_id, len(content), len(parsed))
                sources.extend(parsed)

        elif tool_call_id in fetchurl_url_map:
            fetch_matched += 1
            fetched_url = fetchurl_url_map[tool_call_id]
            content = flatten_tool_content(event.get("content", ""))
            if content:
                title = fetchurl_source_title(fetched_url, content)
                logger.info("extract_search_sources[FetchURL]: id=%s url=%s content=%d chars title=%r",
                            tool_call_id, fetched_url, len(content), title[:80])
                sources.append({"url": fetched_url, "title": title, "content": content})

    logger.info("extract_search_sources: tool_messages=%d, search_matched=%d, fetch_matched=%d, total_sources=%d",
                tool_messages_total, search_matched, fetch_matched, len(sources))
    return sources


def extract_model_response(stream_events):
    """Return the last assistant text content block (ignoring think/thought blocks)."""
    final_text_parts = []
    assistant_events_total = 0
    candidate_events = 0
    for event in stream_events:
        if event.get("role") != "assistant":
            continue
        assistant_events_total += 1
        content = event.get("content", [])
        if isinstance(content, str):
            if not event.get("tool_calls") and content.strip():
                final_text_parts = [content]
                candidate_events += 1
            continue
        if not isinstance(content, list):
            continue
        text_parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
        ]
        if text_parts and not event.get("tool_calls") and any(part.strip() for part in text_parts):
            final_text_parts = text_parts
            candidate_events += 1
    final_text = "\n".join(final_text_parts).strip()
    logger.info("extract_model_response: assistant_events=%d candidate_text_events=%d final_text=%d chars",
                assistant_events_total, candidate_events, len(final_text))
    return final_text


def extract_markdown_link_annotations(model_text, search_sources):
    """Extract url_citation annotations from markdown links [title](url) in the model response."""
    search_url_set = {source["url"] for source in search_sources}
    search_title_map = {source["url"]: source["title"] for source in search_sources}
    annotations = []

    for match in re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", model_text):
        link_url = match.group(2)
        link_title = match.group(1)
        matched_url = link_url if link_url in search_url_set else next(
            (source_url for source_url in search_url_set if link_url in source_url or source_url in link_url),
            link_url,
        )
        annotations.append({
            "type": "url_citation",
            "start_index": match.start(),
            "end_index": match.end(),
            "url": matched_url,
            "title": search_title_map.get(matched_url, link_title),
        })

    return annotations


def extract_bare_url_annotations(model_text, search_sources, linked_spans):
    """Annotate bare http(s):// URLs in the text that aren't already inside a markdown link."""
    search_title_map = {source["url"]: source["title"] for source in search_sources}
    annotations = []
    for match in re.finditer(r"https?://[^\s)\]>]+", model_text):
        start_index = match.start()
        if any(linked_start <= start_index < linked_end for linked_start, linked_end in linked_spans):
            continue
        url = match.group(0)
        domain = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
        annotations.append({
            "type": "url_citation",
            "start_index": start_index,
            "end_index": match.end(),
            "url": url,
            "title": search_title_map.get(url, domain),
        })
    return annotations


def build_annotations(model_text, search_sources):
    """Build url_citation annotations from markdown links and bare URLs in the model response."""
    markdown_annotations = extract_markdown_link_annotations(model_text, search_sources)
    linked_spans = {(annotation["start_index"], annotation["end_index"]) for annotation in markdown_annotations}
    bare_annotations = extract_bare_url_annotations(model_text, search_sources, linked_spans)
    combined = markdown_annotations + bare_annotations

    seen_keys = set()
    unique_annotations = [
        annotation for annotation in combined
        if (key := (annotation["url"], annotation["start_index"], annotation["end_index"])) not in seen_keys
        and not seen_keys.add(key)
    ]

    return sorted(unique_annotations, key=lambda annotation: (annotation["start_index"], annotation["end_index"]))


def build_openai_format(search_queries, search_sources, model_text):
    """Build OpenAI Responses API-style output from parsed Kimi stream data."""
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
        annotations = build_annotations(model_text, search_sources)
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
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
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
    parser.add_argument("--timeout", type=int, default=180, help="Timeout in seconds")
    parser.add_argument("--raw-dir", default=KIMI_DEFAULT_OUTPUT_DIR, help="Directory for output files")
    return parser


def main():
    """CLI entry point for standalone Kimi search."""
    from llm_search.logging_setup import setup_colorized_logging

    parser = build_argument_parser()
    args = parser.parse_args()
    setup_colorized_logging(verbose=args.verbose)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
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
