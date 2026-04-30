"""Stream-json + tool-output parsers for the Kimi provider.

Pure parsing layer: takes the raw stdout from the Kimi CLI and turns it into
OpenAI Responses-API-style output structures. No subprocess, no I/O. Imported
by providers/kimi.py for the runtime path and by tests for direct unit checks.
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

SEARCH_TOOL_NAMES = {"SearchWeb"}
FETCH_TOOL_NAMES = {"FetchURL"}
GROUNDING_TOOL_NAMES = SEARCH_TOOL_NAMES | FETCH_TOOL_NAMES
FETCHURL_SYSTEM_META_PATTERN = re.compile(r"<system>.*?</system>\s*", re.DOTALL)

TITLE_ANCHOR_PATTERN = re.compile(r"^Title:[ \t]*([^\n]*?)[ \t]*$", re.MULTILINE)
URL_FIELD_PATTERN = re.compile(r"^URL:[ \t]*(\S+)[ \t]*$", re.MULTILINE)
DATE_FIELD_PATTERN = re.compile(r"^Date:[ \t]*([^\n]*?)[ \t]*$", re.MULTILINE)
SUMMARY_FIELD_PATTERN = re.compile(r"^Summary:[ \t]*(.*)", re.MULTILINE | re.DOTALL)


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
    """Parse a SearchWeb result body into a list of {url, title, date, content} entries.

    Records are bounded by ^Title: anchors rather than \\n---\\n splits so a Summary
    that contains a markdown horizontal rule doesn't silently drop subsequent entries.
    """
    entries = []
    title_matches = list(TITLE_ANCHOR_PATTERN.finditer(tool_content_text))
    for index, title_match in enumerate(title_matches):
        block_start = title_match.start()
        block_end = title_matches[index + 1].start() if index + 1 < len(title_matches) else len(tool_content_text)
        block = tool_content_text[block_start:block_end]

        url_match = URL_FIELD_PATTERN.search(block)
        if not url_match:
            continue
        date_match = DATE_FIELD_PATTERN.search(block)
        summary_match = SUMMARY_FIELD_PATTERN.search(block)
        summary_text = summary_match.group(1).strip() if summary_match else ""
        summary_text = re.sub(r"\n*---+\s*\Z", "", summary_text).strip()

        entries.append({
            "url": url_match.group(1).strip(),
            "title": title_match.group(1).strip(),
            "date": date_match.group(1).strip() if date_match else "",
            "content": summary_text,
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


def detect_provider_failure(stream_events, model_response):
    """Return an error message if the Kimi run failed, else None.

    Kimi-cli emits role=="error" or type=="error" events on auth/rate-limit/CLI-crash.
    A clean crash also produces an empty model_response — also a failure.
    """
    for event in stream_events:
        if event.get("role") == "error":
            return event.get("content") or "<kimi role=error event with no content>"
        if (event.get("type") or "").lower() in {"error", "turn.failed"}:
            return event.get("message") or "<kimi error event with no message>"
    if not model_response:
        return "kimi returned empty model response (auth/rate-limit/CLI-crash likely)"
    return None


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
    """Build url_citation annotations from markdown links, falling back to bare URLs only when markdown pass is empty."""
    markdown_annotations = extract_markdown_link_annotations(model_text, search_sources)
    if markdown_annotations:
        annotations = markdown_annotations
    else:
        annotations = extract_bare_url_annotations(model_text, search_sources, set())

    seen_keys = set()
    unique_annotations = [
        annotation for annotation in annotations
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
