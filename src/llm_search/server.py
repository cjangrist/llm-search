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


def read_provider_files(provider, output_dir, started_at):
    """Read intermediate files written by the provider during this request."""
    prefix_map = {
        "codex": ["codex_raw_*.jsonl", "codex_trace_*.log", "codex_search_*.json"],
        "claude": ["claude_raw_*.json", "claude_search_*.json"],
        "gemini": ["gemini_raw_*.json", "gemini_grounding_*.json", "gemini_activity_*.log"],
    }
    file_contents = {}
    for pattern in prefix_map.get(provider, []):
        for filepath in glob.glob(os.path.join(output_dir, pattern)):
            if os.path.getmtime(filepath) >= started_at:
                try:
                    with open(filepath) as file_handle:
                        content = file_handle.read()
                    try:
                        file_contents[os.path.basename(filepath)] = json.loads(content)
                    except json.JSONDecodeError:
                        file_contents[os.path.basename(filepath)] = content
                except OSError:
                    pass
    return file_contents


def write_request_log(provider, model_name, prompt, request_body, response_body, latency_seconds, error, output_dir, started_at):
    """Write a unified JSON request/response log to LOGS_DIR."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    timestamp_str = datetime.fromtimestamp(started_at, tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(LOGS_DIR, f"request_{provider}_{timestamp_str}.json")

    provider_files = read_provider_files(provider, output_dir, started_at)

    log_entry = {
        "timestamp_utc": datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat(),
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


def create_app():
    """Create and configure the Flask application."""
    flask_app = Flask(__name__)

    @flask_app.route("/v1/chat/completions", methods=["POST"])
    def chat_completions():
        """OpenAI-compatible Chat Completions endpoint with web search grounding.

        Accepts any API key (or none) in the Authorization header for
        compatibility with OpenAI client libraries that require a key.

        Request body:
            model (str): "provider/model_name" (e.g. "claude/haiku")
            messages (list): OpenAI-format messages array
            timeout (int, optional): CLI timeout in seconds

        Returns:
            OpenAI Chat Completions JSON with url_citation annotations.
        """
        body = request.get_json(force=True)

        model_string = body.get("model")
        parsed, error_message = parse_model_field(model_string)
        if error_message:
            return make_error_response(error_message, 400, "model")
        provider, model_name = parsed

        messages = body.get("messages", [])
        if not messages:
            return make_error_response(
                "messages is required and must be a non-empty array", 400, "messages"
            )

        prompt = extract_prompt_from_messages(messages)
        if not prompt:
            return make_error_response(
                "messages must contain at least one user message with content", 400, "messages"
            )

        timeout = body.get("timeout") or PROVIDER_DEFAULTS[provider]["timeout"]

        logger.info("POST /v1/chat/completions model=%s prompt=%s", model_string, prompt[:80])

        started_at = time.time()
        response_body = None
        error_message = None

        try:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            openai_output, model_response_text = PROVIDER_RUNNERS[provider](
                prompt, model_name, OUTPUT_DIR, timeout
            )
            response = build_chat_completion_response(
                model_string, model_response_text, openai_output
            )
            response_body = response
            return jsonify(response)
        except Exception as search_error:
            error_message = str(search_error)
            logger.error("Chat completion failed: %s\n%s", search_error, traceback.format_exc())
            return make_error_response(str(search_error), 500)
        finally:
            latency_seconds = time.time() - started_at
            logger.info("Completed in %.1fs (provider=%s model=%s)", latency_seconds, provider, model_name)
            write_request_log(
                provider, model_name, prompt, body, response_body,
                latency_seconds, error_message, OUTPUT_DIR, started_at,
            )

    @flask_app.route("/health", methods=["GET"])
    def health():
        """Health check endpoint for container probes and load balancers."""
        return jsonify({"status": "ok"})

    @flask_app.route("/providers", methods=["GET"])
    def providers():
        """List configured providers with their default models and timeouts."""
        return jsonify({
            provider: {
                "default_model": defaults["model"],
                "default_timeout": defaults["timeout"],
            }
            for provider, defaults in PROVIDER_DEFAULTS.items()
        })

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
