"""Flask API server with OpenAI-compatible Chat Completions endpoint.

Exposes POST /v1/chat/completions that dispatches to Claude, Codex, or Gemini
providers and returns results in OpenAI Chat Completions format with url_citation
annotations.

Usage:
    python -m llm_search [--port 8080] [--host 0.0.0.0]
    gunicorn llm_search.server:app
"""

import argparse
import glob
import json
import logging
import os
import time
import traceback
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, request

from llm_search.config import HOST, OUTPUT_DIR, PORT, PROVIDER_DEFAULTS
from llm_search.logging_setup import setup_colorized_logging
from llm_search.providers import PROVIDER_RUNNERS
from llm_search.response import build_chat_completion_response

LOGS_DIR = os.path.join(OUTPUT_DIR, "logs")

logger = logging.getLogger(__name__)


def parse_model_field(model_string):
    """Split 'provider/model' into (provider, model_name).

    Also accepts bare provider name (e.g. 'codex') and uses its default model.

    Returns:
        Tuple of ((provider, model_name), None) on success,
        or (None, error_message) on failure.
    """
    if not model_string:
        return None, (
            "model must be in format 'provider/model' or just 'provider' "
            "(e.g. 'codex', 'claude/haiku', 'gemini/gemini-3-flash-preview')"
        )
    if "/" not in model_string:
        if model_string in PROVIDER_RUNNERS:
            return (model_string, PROVIDER_DEFAULTS[model_string]["model"]), None
        return None, f"unknown provider '{model_string}', must be one of: {list(PROVIDER_RUNNERS.keys())}"
    provider, model_name = model_string.split("/", 1)
    if provider not in PROVIDER_RUNNERS:
        return None, f"unknown provider '{provider}', must be one of: {list(PROVIDER_RUNNERS.keys())}"
    return (provider, model_name), None


PROVIDER_ARTEFACT_TEMPLATES = {
    # codex_trace_*.log is intentionally NOT embedded — it contains every HTTP
    # request/response from RUST_LOG=codex_api=trace, including the
    # `Authorization: Bearer …` header. Persisting it into the bind-mounted
    # request log would leak the token to the host. The trace file still lives
    # inside the container at /tmp/llm-search/ for parse_trace_log_sse_events
    # to read during the request — it just doesn't escape to the host.
    "codex": ["codex_raw_{rid}.jsonl", "codex_search_{rid}.json"],
    "claude": ["claude_raw_{rid}.json", "claude_search_{rid}.json"],
    "gemini": ["gemini_raw_{rid}.json", "gemini_grounding_{rid}.json", "gemini_activity_{rid}.jsonl"],
    "kimi": ["kimi_raw_{rid}.json", "kimi_search_{rid}.json", "kimi_stderr_{rid}.log"],
}


def read_provider_files(provider, output_dir, request_id):
    """Read intermediate files for this exact request_id (no glob, no mtime filter)."""
    file_contents = {}
    for filename_template in PROVIDER_ARTEFACT_TEMPLATES.get(provider, []):
        filepath = os.path.join(output_dir, filename_template.format(rid=request_id))
        if not os.path.isfile(filepath):
            continue
        try:
            with open(filepath) as file_handle:
                content = file_handle.read()
        except OSError as read_error:
            logger.warning("Failed to read provider artefact %s: %s", filepath, read_error)
            continue
        try:
            file_contents[os.path.basename(filepath)] = json.loads(content)
        except json.JSONDecodeError:
            file_contents[os.path.basename(filepath)] = content
    return file_contents


def write_request_log(provider, model_name, prompt, request_body, response_body, latency_seconds, error, output_dir, started_at, request_id):
    """Write a unified JSON request/response log to LOGS_DIR."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    timestamp_str = datetime.fromtimestamp(started_at, tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(LOGS_DIR, f"request_{provider}_{timestamp_str}_{request_id}.json")

    provider_files = read_provider_files(provider, output_dir, request_id)

    log_entry = {
        "timestamp_utc": datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat(),
        "request_id": request_id,
        "provider": provider,
        "model": model_name,
        "latency_seconds": round(latency_seconds, 3),
        "request": request_body,
        "response": response_body,
        "error": error,
        "provider_files": provider_files,
    }

    with open(log_path, "w") as log_file:
        json.dump(log_entry, log_file, indent=2)

    logger.info("Request log written to %s (%.1fs)", log_path, latency_seconds)
    return log_path


def extract_prompt_from_messages(messages):
    """Extract the last user message content as the search prompt.

    Supports both string content and the array-of-parts format.
    """
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content", "")
            if isinstance(content, list):
                text_parts = [
                    part.get("text", "")
                    for part in content
                    if part.get("type") == "text"
                ]
                return " ".join(text_parts)
            return content
    return None


def make_error_response(message, status_code, param=None):
    """Build an OpenAI-style error response."""
    return jsonify({
        "error": {
            "message": message,
            "type": "invalid_request_error",
            "param": param,
            "code": None,
        }
    }), status_code


MAX_TIMEOUT_SECONDS = 290


def validate_request_body(body):
    """Validate the parsed JSON body. Returns (valid_dict, error_response) — exactly one is non-None."""
    if not isinstance(body, dict):
        return None, make_error_response("request body must be a JSON object", 400, None)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return None, make_error_response("messages is required and must be a non-empty array", 400, "messages")
    if not all(isinstance(message, dict) for message in messages):
        return None, make_error_response("each entry in messages must be a JSON object", 400, "messages")
    raw_timeout = body.get("timeout")
    if raw_timeout is not None and (not isinstance(raw_timeout, int) or isinstance(raw_timeout, bool) or not 1 <= raw_timeout <= MAX_TIMEOUT_SECONDS):
        return None, make_error_response(f"timeout must be an integer in [1, {MAX_TIMEOUT_SECONDS}] seconds", 400, "timeout")
    return body, None


def handle_chat_completions():
    """OpenAI-compatible Chat Completions endpoint with web search grounding.

    Request body:
        model (str): "provider/model_name" (e.g. "claude/haiku")
        messages (list): OpenAI-format messages array
        timeout (int, optional): CLI timeout in seconds
    """
    body = request.get_json(silent=True, force=True)
    body, error_response = validate_request_body(body)
    if error_response is not None:
        return error_response

    parsed, parse_error = parse_model_field(body.get("model"))
    if parse_error:
        return make_error_response(parse_error, 400, "model")
    provider, model_name = parsed

    prompt = extract_prompt_from_messages(body["messages"])
    if not prompt:
        return make_error_response("messages must contain at least one user message with content", 400, "messages")

    timeout = body.get("timeout") or PROVIDER_DEFAULTS[provider]["timeout"]
    model_string = body.get("model")
    request_id = uuid.uuid4().hex[:12]
    logger.info("POST /v1/chat/completions request_id=%s model=%s prompt=%s", request_id, model_string, prompt[:80])

    started_at = time.time()
    response_body = None
    runtime_error = None
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        openai_output, model_response_text = PROVIDER_RUNNERS[provider](prompt, model_name, OUTPUT_DIR, timeout, request_id)
        response_body = build_chat_completion_response(model_string, model_response_text, openai_output)
        return jsonify(response_body)
    except Exception as search_error:
        runtime_error = str(search_error)
        logger.error("Chat completion failed (request_id=%s): %s\n%s", request_id, search_error, traceback.format_exc())
        return make_error_response(f"internal error (request_id={request_id})", 500)
    finally:
        latency_seconds = time.time() - started_at
        logger.info("Completed in %.1fs (request_id=%s provider=%s model=%s)", latency_seconds, request_id, provider, model_name)
        try:
            write_request_log(provider, model_name, prompt, body, response_body,
                              latency_seconds, runtime_error, OUTPUT_DIR, started_at, request_id)
        except Exception as log_error:
            logger.error("write_request_log failed (request_id=%s): %s", request_id, log_error)


def handle_health():
    """Health check endpoint for container probes and load balancers."""
    return jsonify({"status": "ok"})


def handle_providers():
    """List configured providers with their default models and timeouts."""
    return jsonify({
        provider_name: {
            "default_model": defaults["model"],
            "default_timeout": defaults["timeout"],
        }
        for provider_name, defaults in PROVIDER_DEFAULTS.items()
    })


MAX_REQUEST_BYTES = 256 * 1024


def create_app():
    """Create and configure the Flask application by binding module-level handlers."""
    flask_app = Flask(__name__)
    flask_app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    flask_app.add_url_rule("/v1/chat/completions", view_func=handle_chat_completions, methods=["POST"])
    flask_app.add_url_rule("/health", view_func=handle_health, methods=["GET"])
    flask_app.add_url_rule("/providers", view_func=handle_providers, methods=["GET"])
    return flask_app


setup_colorized_logging()
app = create_app()


def main():
    """CLI entry point for running the development server."""
    parser = argparse.ArgumentParser(description="LLM Search Chat Completions API")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--host", default=HOST)
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    logger.info("Starting LLM Search API on %s:%d", args.host, args.port)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
