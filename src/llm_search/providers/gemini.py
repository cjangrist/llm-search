"""Gemini CLI integration for web search extraction.

Parses Gemini CLI's activity log for grounding metadata, resolves Vertex AI
redirect URIs, and extracts citations in OpenAI Responses API format.

Usage: python -m llm_search.providers.gemini "your prompt" [-m model] [--raw-dir /tmp]
"""

import argparse
import json
import logging
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import sh

from llm_search.config import (
    GEMINI_DEFAULT_MODEL,
    GEMINI_DEFAULT_OUTPUT_DIR,
    GEMINI_SANDBOX_DIR,
    GEMINI_SCRIPT_PATH,
    PROVIDER_DEFAULTS,
    VERTEX_REDIRECT_PREFIX,
)
from llm_search.prompts import load_system_prompt
from llm_search.providers.subprocess_safety import build_sanitized_environment, kill_subprocess_tree_on_done

logger = logging.getLogger(__name__)

PROMPTS_DIRECTORY = os.path.dirname(os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "prompts", "system_prompt.md")
))
SYSTEM_PROMPT_FILE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts", "system_prompt.md"
)


REDIRECT_PER_URI_TIMEOUT_SECONDS = 5
REDIRECT_OVERALL_TIMEOUT_SECONDS = 30
REDIRECT_PARALLELISM = 4
# Curl exit codes we tolerate silently; anything else gets logged.
# 0=success, 6=DNS resolve, 7=connect, 28=timeout, 35/56/60=TLS/recv issues.
CURL_TOLERATED_EXIT_CODES = {0, 6, 7, 28, 35, 52, 56, 60}


def resolve_redirect(uri):
    """Follow a Vertex grounding redirect to get the actual URL via curl."""
    if not uri or not uri.startswith(VERTEX_REDIRECT_PREFIX):
        return uri
    try:
        result = sh.curl(
            "-sIL", "-o", "/dev/null",
            "-w", "%{url_effective}",
            "--max-time", str(REDIRECT_PER_URI_TIMEOUT_SECONDS),
            uri,
            _ok_code=list(CURL_TOLERATED_EXIT_CODES),
        )
        resolved = str(result).strip()
        return resolved if resolved and resolved != uri else uri
    except sh.ErrorReturnCode as curl_error:
        logger.warning("resolve_redirect curl unexpected exit %d for %s", curl_error.exit_code, uri[:120])
        return uri
    except Exception as resolve_error:
        logger.warning("resolve_redirect failed for %s: %s", uri[:120], resolve_error)
        return uri


def resolve_all_uris(uri_list):
    """Resolve redirect URIs in parallel with a hard overall budget.

    Hung redirects no longer stretch the request by minutes — overall budget is bounded
    by REDIRECT_OVERALL_TIMEOUT_SECONDS and parallelism is capped at REDIRECT_PARALLELISM
    so we don't hammer the same redirect host with too many concurrent connections.
    """
    unique_redirect_uris = list({uri for uri in uri_list if uri and uri.startswith(VERTEX_REDIRECT_PREFIX)})
    if not unique_redirect_uris:
        return {}
    logger.debug("Resolving %d unique redirect URIs", len(unique_redirect_uris))
    resolved_map = {}
    with ThreadPoolExecutor(max_workers=REDIRECT_PARALLELISM) as executor:
        pending_futures = {executor.submit(resolve_redirect, uri): uri for uri in unique_redirect_uris}
        try:
            for completed_future in as_completed(pending_futures, timeout=REDIRECT_OVERALL_TIMEOUT_SECONDS):
                original_uri = pending_futures[completed_future]
                resolved_map[original_uri] = completed_future.result()
        except TimeoutError:
            logger.warning(
                "resolve_all_uris hit overall %ds budget — leaving %d/%d URIs unresolved",
                REDIRECT_OVERALL_TIMEOUT_SECONDS,
                len(unique_redirect_uris) - len(resolved_map),
                len(unique_redirect_uris),
            )
            for future, original_uri in pending_futures.items():
                if original_uri not in resolved_map:
                    resolved_map.setdefault(original_uri, original_uri)
                    future.cancel()
    unresolved_count = sum(1 for value in resolved_map.values() if value.startswith(VERTEX_REDIRECT_PREFIX))
    logger.debug("Resolved %d/%d URIs", len(resolved_map) - unresolved_count, len(resolved_map))
    return resolved_map


def safe_which(binary_name):
    """Return the absolute path to `binary_name` on PATH, or None if not found.

    sh.which raises sh.ErrorReturnCode_1 on miss instead of returning None — wrap
    so callers can fall back cleanly without try/except scattered through the call sites.
    """
    try:
        path = str(sh.which(binary_name)).strip()
    except sh.ErrorReturnCode:
        return None
    return path or None


def find_gemini_script(script_path_override):
    """Locate the gemini CLI entry point for running under bun.

    Preference order: explicit script_path_override (config-supplied), else resolve
    the `gemini` binary from PATH, dereferencing symlinks (npm shims). Returns None
    if no gemini binary is on PATH and no override is provided.
    """
    if script_path_override and os.path.isfile(script_path_override):
        return script_path_override
    gemini_bin = safe_which("gemini")
    if gemini_bin is None:
        return None
    if os.path.islink(gemini_bin):
        return os.path.realpath(gemini_bin)
    return gemini_bin


_BUN_AVAILABLE = None


def is_bun_available():
    """Cached check for `bun` on PATH so we don't fork+exec `which` per request."""
    global _BUN_AVAILABLE
    if _BUN_AVAILABLE is None:
        _BUN_AVAILABLE = safe_which("bun") is not None
    return _BUN_AVAILABLE


GEMINI_CLI_FLAGS = ("-o", "stream-json", "--yolo", "--skip-trust")


def invoke_gemini_via_bun(model, augmented_prompt, gemini_environment, sandbox_dir, timeout_seconds, script_path):
    """Run gemini-cli under bun (faster cold-start than node)."""
    gemini_script = find_gemini_script(script_path)
    logger.debug("Running gemini via bun: %s", gemini_script)
    return sh.bun(
        gemini_script, "-m", model, "-p", augmented_prompt, *GEMINI_CLI_FLAGS,
        _env=gemini_environment, _cwd=sandbox_dir,
        _ok_code=[0, 1], _encoding="utf-8", _timeout=timeout_seconds,
        _new_session=True, _done=kill_subprocess_tree_on_done(),
    )


def invoke_gemini_via_node(model, augmented_prompt, gemini_environment, sandbox_dir, timeout_seconds, script_path):
    """Run gemini-cli under node (fallback when bun isn't on PATH). script_path unused on this branch."""
    logger.debug("bun not found, falling back to node runtime")
    return sh.gemini(
        "-m", model, "-p", augmented_prompt, *GEMINI_CLI_FLAGS,
        _env=gemini_environment, _cwd=sandbox_dir,
        _ok_code=[0, 1], _encoding="utf-8", _timeout=timeout_seconds,
        _new_session=True, _done=kill_subprocess_tree_on_done(),
    )


def call_gemini(prompt, model, output_dir, timeout_seconds, environment, sandbox_dir, script_path, request_id=None):
    """Call Gemini CLI (via bun if available, else node), return raw text and activity log path."""
    logger.debug("call_gemini(model=%s, output_dir=%s, timeout=%ds, request_id=%s)", model, output_dir, timeout_seconds, request_id)
    if request_id is None:
        request_id = uuid.uuid4().hex[:12]
    activity_log_path = os.path.join(output_dir, f"gemini_activity_{request_id}.jsonl")
    # Gemini-cli authenticates via gcloud Vertex creds, not env. Strip credential-shaped
    # env vars (ANTHROPIC_*, KIMI_API_KEY, etc.) so a prompt-or-tool escape inside the CLI
    # cannot exfiltrate keys for the other providers from the same gunicorn process.
    gemini_environment = {
        **build_sanitized_environment(environment),
        "GEMINI_CLI_ACTIVITY_LOG_TARGET": activity_log_path,
        "GEMINI_SYSTEM_MD": SYSTEM_PROMPT_FILE_PATH,
    }
    augmented_prompt = f'CRITICAL RULE-> using web_search answer: "{prompt}"'

    os.makedirs(sandbox_dir, exist_ok=True)

    runner = invoke_gemini_via_bun if is_bun_available() else invoke_gemini_via_node
    raw_output = runner(model, augmented_prompt, gemini_environment, sandbox_dir, timeout_seconds, script_path)

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
    with open(activity_log_path, encoding="utf-8", errors="replace") as activity_file:
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

    if grounding_blocks:
        # Emit a single message item carrying the concatenated text. Each block's
        # annotations have offsets relative to that block's own text — shift them
        # by the running prefix length when joining so they point at the right
        # span in the joined output. Without this shift annotations from blocks 1+
        # point at offsets that don't exist in the final text.
        joined_parts = []
        joined_annotations = []
        running_prefix_len = 0
        block_separator = "\n"
        for block_index, block in enumerate(grounding_blocks):
            block_text = block["text"] or ""
            block_annotations = build_annotations(block_text, block["metadata"], uri_resolution_map)
            for annotation in block_annotations:
                shifted = dict(annotation)
                shifted["start_index"] = annotation.get("start_index", 0) + running_prefix_len
                shifted["end_index"] = annotation.get("end_index", 0) + running_prefix_len
                joined_annotations.append(shifted)
            joined_parts.append(block_text)
            running_prefix_len += len(block_text) + (len(block_separator) if block_index < len(grounding_blocks) - 1 else 0)
        output.append({
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": block_separator.join(joined_parts),
                    "annotations": joined_annotations,
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
    """Get model response text from grounding blocks or stream events.

    Mirrors the join order in build_openai_format EXACTLY (every block, including
    `text=None` placeholders rendered as ""). Without this exact match the joined
    string and the annotation offsets emitted to the client diverge — annotations
    can overrun the content length when an earlier "thought-only" block has text=None.
    """
    if grounding_blocks:
        return "\n".join((block["text"] or "") for block in grounding_blocks)
    return " ".join(
        event.get("content", "")
        for event in stream_events
        if event.get("type") == "message" and event.get("role") == "assistant"
    )


def detect_provider_failure(stream_events, grounding_blocks, model_response):
    """Return an error message if the Gemini run failed, else None.

    Gemini-cli emits stream events with type=="error" or "turn.failed" when the
    upstream API rejects the call (auth, rate limit, model-not-found). On a clean
    crash the stream simply ends with no grounding and no model_response — also a
    failure we should surface as HTTP 500 instead of an HTTP 200 with empty content.
    """
    for event in stream_events:
        event_type = (event.get("type") or "").lower()
        if event_type in {"error", "turn.failed"}:
            # event["error"] can be a dict, None, or string. Coerce to dict only when it IS one.
            error_field = event.get("error")
            error_field_dict = error_field if isinstance(error_field, dict) else {}
            return _ensure_failure_string(
                event.get("message")
                or error_field_dict.get("message")
                or (error_field if isinstance(error_field, str) else None),
                "<gemini error event with no message>",
            )
        if event.get("is_error"):
            return _ensure_failure_string(
                event.get("error") or event.get("content"),
                "<gemini event flagged is_error=true>",
            )
    # Filter out "thought-only" blocks (text=None) before deciding the response is real.
    # A response made up entirely of thought-only blocks contributes zero text to the client
    # — same end-user effect as no grounding at all, surface as HTTP 500.
    blocks_with_text = [block for block in grounding_blocks if block.get("text")]
    if not model_response and not blocks_with_text:
        return "gemini returned empty response with no grounding text (auth/rate-limit/CLI-crash likely)"
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


def make_request_sandbox(parent_sandbox_dir, request_id, ignore_pattern=None):
    """Create a per-request subdir under parent_sandbox_dir; optionally seed an ignore-pattern file."""
    request_sandbox = os.path.join(parent_sandbox_dir, request_id)
    os.makedirs(request_sandbox, mode=0o700, exist_ok=True)
    if ignore_pattern is not None:
        for filename, content in ignore_pattern.items():
            with open(os.path.join(request_sandbox, filename), "w") as ignore_file:
                ignore_file.write(content)
    return request_sandbox


def discard_request_sandbox(request_sandbox, parent_sandbox_dir):
    """Move a used request sandbox into a date-stamped .trash subdir (no rm; RULE_07).

    Cleanup must never throw — we run from a `finally:` block where an unexpected raise
    masks the original provider exception. Catch ALL OSErrors (ENOSPC, EACCES, RO-FS).
    """
    try:
        if not request_sandbox or not os.path.isdir(request_sandbox):
            return
        trash_dir = os.path.join(parent_sandbox_dir, ".trash", datetime.now().strftime("%Y%m%d"))
        os.makedirs(trash_dir, mode=0o700, exist_ok=True)
        trashed = os.path.join(trash_dir, os.path.basename(request_sandbox))
        os.replace(request_sandbox, trashed)
    except OSError as cleanup_error:
        logger.warning("discard_request_sandbox: cleanup failed for %s: %s", request_sandbox, cleanup_error)


def run_search(prompt, model, output_dir, timeout, request_id=None, environment=None, sandbox_dir=None, script_path=None):
    """Run Gemini web search and return OpenAI-format result.

    Args:
        prompt: The user's search query.
        model: Gemini model name (e.g. "search-fast").
        output_dir: Directory to save intermediate files.
        timeout: CLI timeout in seconds.
        request_id: Per-request id used as the artefact filename suffix (defaults to a fresh uuid hex).
        environment: Process environment dict (defaults to os.environ).
        sandbox_dir: CWD for the gemini CLI subprocess (defaults to GEMINI_SANDBOX_DIR config).
        script_path: Override gemini-cli's bundled JS entrypoint (defaults to GEMINI_SCRIPT_PATH config).

    Returns:
        Tuple of (openai_output_list, model_response_text).
    """
    logger.debug("run_search(model=%s, timeout=%d, request_id=%s)", model, timeout, request_id)
    if request_id is None:
        request_id = uuid.uuid4().hex[:12]
    raw_json_path = os.path.join(output_dir, f"gemini_raw_{request_id}.json")
    grounding_json_path = os.path.join(output_dir, f"gemini_grounding_{request_id}.json")

    parent_sandbox = sandbox_dir if sandbox_dir is not None else GEMINI_SANDBOX_DIR
    request_sandbox = make_request_sandbox(parent_sandbox, request_id, ignore_pattern={".geminiignore": "*\n"})
    try:
        raw_text, activity_log_path = call_gemini(
            prompt, model, output_dir, timeout,
            environment if environment is not None else os.environ,
            request_sandbox,
            script_path if script_path is not None else GEMINI_SCRIPT_PATH,
            request_id,
        )
    finally:
        discard_request_sandbox(request_sandbox, parent_sandbox)
    stream_events = parse_stream_events(raw_text)
    with open(raw_json_path, "w") as output_file:
        json.dump(stream_events, output_file, indent=2)

    search_queries, grounding_blocks = parse_activity_log(activity_log_path)
    model_response = extract_model_response(grounding_blocks, stream_events)

    failure_message = detect_provider_failure(stream_events, grounding_blocks, model_response)
    if failure_message:
        logger.error("Gemini run failed: %s", failure_message[:240])
        raise RuntimeError(f"gemini provider failed: {failure_message}")

    openai_output = build_openai_format(search_queries, grounding_blocks, True)
    with open(grounding_json_path, "w") as output_file:
        json.dump(openai_output, output_file, indent=2)

    logger.debug("run_search returning %d chars response", len(model_response))
    return openai_output, model_response


def build_argument_parser():
    """Build CLI argument parser for standalone usage."""
    parser = argparse.ArgumentParser(description="Query Gemini CLI and extract search citations.")
    parser.add_argument("prompt", help="The prompt to send to Gemini")
    parser.add_argument("-m", "--model", default=GEMINI_DEFAULT_MODEL)
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging")
    parser.add_argument("--timeout", type=int, default=PROVIDER_DEFAULTS["gemini"]["timeout"], help="Timeout in seconds")
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
    raw_text, activity_log_path = call_gemini(
        args.prompt, args.model, args.raw_dir, args.timeout,
        os.environ, GEMINI_SANDBOX_DIR, GEMINI_SCRIPT_PATH,
    )

    stream_events = parse_stream_events(raw_text)
    with open(raw_json_path, "w") as output_file:
        json.dump(stream_events, output_file, indent=2)
    logger.info("Raw stream-json saved to %s", raw_json_path)

    search_queries, grounding_blocks = parse_activity_log(activity_log_path)
    logger.debug("Found %d search queries, %d grounding blocks", len(search_queries), len(grounding_blocks))

    model_response = extract_model_response(grounding_blocks, stream_events)

    failure_message = detect_provider_failure(stream_events, grounding_blocks, model_response)
    if failure_message:
        logger.error("Gemini run failed: %s", failure_message[:240])
        sys.exit(2)

    openai_output = build_openai_format(search_queries, grounding_blocks, not args.no_resolve)
    with open(grounding_json_path, "w") as output_file:
        json.dump(openai_output, output_file, indent=2)
    logger.info("Grounding data saved to %s", grounding_json_path)
    logger.info("Activity log at %s", activity_log_path)

    print(model_response)


if __name__ == "__main__":
    main()
