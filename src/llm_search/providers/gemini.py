"""Gemini CLI integration for web search extraction.

Parses Gemini CLI's activity log for grounding metadata, resolves Vertex AI
redirect URIs, and extracts citations in OpenAI Responses API format.

Usage: python -m llm_search.providers.gemini "your prompt" [-m model] [--raw-dir /tmp]
"""

import argparse
import json
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import sh

from llm_search.config import (
    GEMINI_DEFAULT_MODEL,
    GEMINI_DEFAULT_OUTPUT_DIR,
    GEMINI_SANDBOX_DIR,
    GEMINI_SCRIPT_PATH,
    VERTEX_REDIRECT_PREFIX,
)
from llm_search.prompts import load_system_prompt

logger = logging.getLogger(__name__)

PROMPTS_DIRECTORY = os.path.dirname(os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "prompts", "system_prompt.md")
))
SYSTEM_PROMPT_FILE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts", "system_prompt.md"
)


def resolve_redirect(uri):
    """Follow a Vertex grounding redirect to get the actual URL via curl."""
    if not uri or not uri.startswith(VERTEX_REDIRECT_PREFIX):
        return uri
    try:
        result = sh.curl(
            "-sIL", "-o", "/dev/null",
            "-w", "%{url_effective}",
            "--max-time", "10",
            uri,
            _ok_code=range(256),
        )
        resolved = str(result).strip()
        return resolved if resolved and resolved != uri else uri
    except Exception:
        return uri


def resolve_all_uris(uri_list):
    """Resolve redirect URIs in parallel."""
    unique_redirect_uris = list({uri for uri in uri_list if uri and uri.startswith(VERTEX_REDIRECT_PREFIX)})
    if not unique_redirect_uris:
        return {}
    logger.debug("Resolving %d unique redirect URIs", len(unique_redirect_uris))
    resolved_map = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        pending_futures = {executor.submit(resolve_redirect, uri): uri for uri in unique_redirect_uris}
        for completed_future in as_completed(pending_futures):
            original_uri = pending_futures[completed_future]
            resolved_map[original_uri] = completed_future.result()
    unresolved_count = sum(1 for value in resolved_map.values() if value.startswith(VERTEX_REDIRECT_PREFIX))
    logger.debug("Resolved %d/%d URIs", len(resolved_map) - unresolved_count, len(resolved_map))
    return resolved_map


def find_gemini_script():
    """Locate the gemini CLI entry point for running under bun."""
    if GEMINI_SCRIPT_PATH and os.path.isfile(GEMINI_SCRIPT_PATH):
        return GEMINI_SCRIPT_PATH
    gemini_bin = str(sh.which("gemini")).strip()
    if os.path.islink(gemini_bin):
        return os.path.realpath(gemini_bin)
    return gemini_bin


def call_gemini(prompt, model, output_dir, timeout_seconds=180):
    """Call Gemini CLI via bun for faster startup, return raw text and activity log path."""
    logger.debug("call_gemini(model=%s, output_dir=%s, timeout=%ds)", model, output_dir, timeout_seconds)
    activity_log_path = os.path.join(
        output_dir,
        f"gemini_activity_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.jsonl",
    )
    gemini_environment = {
        **os.environ,
        "GEMINI_CLI_ACTIVITY_LOG_TARGET": activity_log_path,
        "GEMINI_SYSTEM_MD": SYSTEM_PROMPT_FILE_PATH,
    }

    augmented_prompt = f'CRITICAL RULE-> using web_search answer: "{prompt}"'

    sandbox_dir = GEMINI_SANDBOX_DIR
    os.makedirs(sandbox_dir, exist_ok=True)

    use_bun = bool(sh.which("bun"))
    if use_bun:
        gemini_script = find_gemini_script()
        logger.debug("Running gemini via bun: %s", gemini_script)
        raw_output = sh.bun(
            gemini_script,
            "-m", model,
            "-p", augmented_prompt,
            "-o", "stream-json",
            "--yolo",
            "--skip-trust",
            _env=gemini_environment,
            _cwd=sandbox_dir,
            _ok_code=[0, 1],
            _encoding="utf-8",
            _timeout=timeout_seconds,
        )
    else:
        logger.debug("bun not found, falling back to node runtime")
        raw_output = sh.gemini(
            "-m", model,
            "-p", augmented_prompt,
            "-o", "stream-json",
            "--yolo",
            "--skip-trust",
            _env=gemini_environment,
            _cwd=sandbox_dir,
            _ok_code=[0, 1],
            _encoding="utf-8",
            _timeout=timeout_seconds,
        )

    raw_text = str(raw_output)
    logger.debug("call_gemini returned %d chars, activity_log=%s", len(raw_text), activity_log_path)
    return raw_text, activity_log_path


def parse_sse_body(body):
    """Parse SSE or plain JSON response body into list of dicts."""
    results = []
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            try:
                results.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    if not results:
        try:
            results.append(json.loads(body))
        except json.JSONDecodeError:
            pass
    return results


def parse_activity_log(activity_log_path):
    """Extract raw tool calls and grounding metadata from activity log."""
    logger.debug("parse_activity_log(%s)", activity_log_path)
    entries = []
    with open(activity_log_path) as activity_file:
        for line in activity_file:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    search_queries = []
    grounding_blocks = []

    for entry in entries:
        if entry.get("type") != "network":
            continue
        response_body = entry.get("payload", {}).get("response", {}).get("body", "")
        if not response_body:
            continue
        for parsed_event in parse_sse_body(response_body):
            if not isinstance(parsed_event, dict):
                continue
            candidates = parsed_event.get("response", parsed_event).get("candidates", [])
            for candidate in candidates:
                content_parts = candidate.get("content", {}).get("parts", [])
                for part in content_parts:
                    function_call = part.get("functionCall", {})
                    if function_call.get("name") == "google_web_search":
                        search_queries.append(function_call.get("args", {}).get("query", ""))
                grounding_metadata = candidate.get("groundingMetadata")
                if not grounding_metadata:
                    continue
                model_text = next(
                    (content_part["text"] for content_part in content_parts if content_part.get("text") and not content_part.get("thought")),
                    None,
                )
                grounding_blocks.append({"metadata": grounding_metadata, "text": model_text})

    logger.debug("parse_activity_log found %d queries, %d grounding blocks", len(search_queries), len(grounding_blocks))
    return search_queries, grounding_blocks


def build_annotations(model_text, grounding_metadata, uri_resolution_map):
    """Convert Gemini groundingSupports into OpenAI-style url_citation annotations."""
    grounding_chunks = grounding_metadata.get("groundingChunks", [])
    grounding_supports = grounding_metadata.get("groundingSupports", [])
    annotations = []

    for support in grounding_supports:
        segment_text = support.get("segment", {}).get("text", "")
        if not segment_text:
            continue

        start_index = model_text.find(segment_text)
        if start_index == -1:
            continue

        end_index = start_index + len(segment_text)
        chunk_indices = support.get("groundingChunkIndices", [])
        confidence_scores = support.get("confidenceScores", [])

        for position, chunk_index in enumerate(chunk_indices):
            if chunk_index >= len(grounding_chunks):
                continue
            web_source = grounding_chunks[chunk_index].get("web", {})
            original_uri = web_source.get("uri", "")
            resolved_uri = uri_resolution_map.get(original_uri, original_uri)
            annotation = {
                "type": "url_citation",
                "start_index": start_index,
                "end_index": end_index,
                "url": resolved_uri,
                "title": web_source.get("title", ""),
            }
            if position < len(confidence_scores):
                annotation["confidence"] = confidence_scores[position]
            annotations.append(annotation)

    seen_keys = set()
    unique_annotations = [
        annotation for annotation in annotations
        if (key := (annotation["url"], annotation["start_index"], annotation["end_index"])) not in seen_keys
        and not seen_keys.add(key)
    ]

    return sorted(unique_annotations, key=lambda annotation: (annotation["start_index"], annotation["end_index"]))


def build_openai_format(search_queries, grounding_blocks, should_resolve):
    """Build OpenAI Responses API-style output from parsed Gemini grounding data."""
    all_chunk_uris = [
        chunk.get("web", {}).get("uri", "")
        for block in grounding_blocks
        for chunk in block["metadata"].get("groundingChunks", [])
    ]
    uri_resolution_map = resolve_all_uris(all_chunk_uris) if should_resolve else {}

    output = []

    combined_queries = list(search_queries)
    for block in grounding_blocks:
        combined_queries.extend(block["metadata"].get("webSearchQueries", []))
    unique_queries = list(dict.fromkeys(combined_queries))

    if unique_queries:
        output.append({
            "type": "web_search_call",
            "status": "completed",
            "action": {
                "type": "search",
                "queries": unique_queries,
            },
        })

    for block in grounding_blocks:
        model_text = block["text"] or ""
        annotations = build_annotations(model_text, block["metadata"], uri_resolution_map)
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


def parse_stream_events(raw_text):
    """Parse raw stream-json text into a list of event dicts."""
    return [
        json.loads(line.strip())
        for line in raw_text.splitlines()
        if line.strip()
        and not line.strip().startswith("YOLO")
        and line.strip().startswith("{")
    ]


def extract_model_response(grounding_blocks, stream_events):
    """Get model response text from grounding blocks or stream events."""
    return next(
        (block["text"] for block in grounding_blocks if block["text"]),
        "".join(
            event.get("content", "")
            for event in stream_events
            if event.get("type") == "message" and event.get("role") == "assistant"
        ),
    )


def run_search(prompt, model, output_dir, timeout):
    """Run Gemini web search and return OpenAI-format result.

    Args:
        prompt: The user's search query.
        model: Gemini model name (e.g. "search-fast").
        output_dir: Directory to save intermediate files.
        timeout: CLI timeout in seconds.

    Returns:
        Tuple of (openai_output_list, model_response_text).
    """
    logger.debug("run_search(model=%s, timeout=%d)", model, timeout)
    timestamp = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    raw_json_path = os.path.join(output_dir, f"gemini_raw_{timestamp}.json")
    grounding_json_path = os.path.join(output_dir, f"gemini_grounding_{timestamp}.json")

    raw_text, activity_log_path = call_gemini(prompt, model, output_dir, timeout)
    stream_events = parse_stream_events(raw_text)
    with open(raw_json_path, "w") as output_file:
        json.dump(stream_events, output_file, indent=2)

    search_queries, grounding_blocks = parse_activity_log(activity_log_path)
    openai_output = build_openai_format(search_queries, grounding_blocks, True)

    with open(grounding_json_path, "w") as output_file:
        json.dump(openai_output, output_file, indent=2)

    model_response = extract_model_response(grounding_blocks, stream_events)
    logger.debug("run_search returning %d chars response", len(model_response))
    return openai_output, model_response


def build_argument_parser():
    """Build CLI argument parser for standalone usage."""
    parser = argparse.ArgumentParser(description="Query Gemini CLI and extract search citations.")
    parser.add_argument("prompt", help="The prompt to send to Gemini")
    parser.add_argument("-m", "--model", default=GEMINI_DEFAULT_MODEL)
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging")
    parser.add_argument("--no-resolve", action="store_true", help="Skip resolving redirect URIs")
    parser.add_argument("--raw-dir", default=GEMINI_DEFAULT_OUTPUT_DIR, help="Directory for output files")
    return parser


def main():
    """CLI entry point for standalone Gemini search."""
    from llm_search.logging_setup import setup_colorized_logging

    parser = build_argument_parser()
    args = parser.parse_args()
    setup_colorized_logging(verbose=args.verbose)

    timestamp = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    raw_json_path = os.path.join(args.raw_dir, f"gemini_raw_{timestamp}.json")
    grounding_json_path = os.path.join(args.raw_dir, f"gemini_grounding_{timestamp}.json")

    logger.info("Calling Gemini model=%s", args.model)
    raw_text, activity_log_path = call_gemini(args.prompt, args.model, args.raw_dir)

    stream_events = parse_stream_events(raw_text)
    with open(raw_json_path, "w") as output_file:
        json.dump(stream_events, output_file, indent=2)
    logger.info("Raw stream-json saved to %s", raw_json_path)

    search_queries, grounding_blocks = parse_activity_log(activity_log_path)
    logger.debug("Found %d search queries, %d grounding blocks", len(search_queries), len(grounding_blocks))

    openai_output = build_openai_format(search_queries, grounding_blocks, not args.no_resolve)
    with open(grounding_json_path, "w") as output_file:
        json.dump(openai_output, output_file, indent=2)
    logger.info("Grounding data saved to %s", grounding_json_path)
    logger.info("Activity log at %s", activity_log_path)

    model_response = extract_model_response(grounding_blocks, stream_events)
    print(model_response)


if __name__ == "__main__":
    main()
