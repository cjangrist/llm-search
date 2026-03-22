"""Flask API server with OpenAI-compatible Chat Completions endpoint.

Exposes POST /v1/chat/completions that dispatches to Claude, Codex, or Gemini
providers and returns results in OpenAI Chat Completions format with url_citation
annotations.

Usage:
    python -m llm_search [--port 8080] [--host 0.0.0.0]
    gunicorn llm_search.server:app
"""

import argparse
import logging
import os
import traceback

from flask import Flask, jsonify, request

from llm_search.config import HOST, OUTPUT_DIR, PORT, PROVIDER_DEFAULTS
from llm_search.logging_setup import setup_colorized_logging
from llm_search.providers import PROVIDER_RUNNERS
from llm_search.response import build_chat_completion_response

logger = logging.getLogger(__name__)


def parse_model_field(model_string):
    """Split 'provider/model' into (provider, model_name).

    Returns:
        Tuple of ((provider, model_name), None) on success,
        or (None, error_message) on failure.
    """
    if not model_string or "/" not in model_string:
        return None, (
            "model must be in format 'provider/model' "
            "(e.g. 'claude/haiku', 'codex/gpt-5.4', 'gemini/gemini-3-flash-preview')"
        )
    provider, model_name = model_string.split("/", 1)
    if provider not in PROVIDER_RUNNERS:
        return None, f"unknown provider '{provider}', must be one of: {list(PROVIDER_RUNNERS.keys())}"
    return (provider, model_name), None


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

        try:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            openai_output, model_response_text = PROVIDER_RUNNERS[provider](
                prompt, model_name, OUTPUT_DIR, timeout
            )
            response = build_chat_completion_response(
                model_string, model_response_text, openai_output
            )
            return jsonify(response)
        except Exception as search_error:
            logger.error("Chat completion failed: %s\n%s", search_error, traceback.format_exc())
            return make_error_response(str(search_error), 500)

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
