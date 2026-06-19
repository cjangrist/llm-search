"""Centralized configuration loaded from environment variables.

All settings are read from environment variables with sensible defaults.
Use a .env file for local development; in Docker, set via Dockerfile ENV.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# --- Server ---
OUTPUT_DIR = os.getenv("LLM_SEARCH_OUTPUT_DIR", "/tmp/llm-search")
PORT = int(os.getenv("LLM_SEARCH_PORT", "8080"))
HOST = os.getenv("LLM_SEARCH_HOST", "0.0.0.0")

# --- Provider defaults ---
PROVIDER_DEFAULTS = {
    "claude": {"model": "haiku", "timeout": 290},
    "codex": {"model": "gpt-5.5", "timeout": 290},
    "gemini": {"model": "Gemini 3.5 Flash (Low)", "timeout": 290},
    "kimi": {"model": "", "timeout": 290},
    "grok": {"model": "grok-build", "timeout": 290},
}

# --- Claude ---
CLAUDE_DEFAULT_MODEL = os.getenv("CLAUDE_MODEL", "haiku")
CLAUDE_DEFAULT_OUTPUT_DIR = os.getenv("CLAUDE_OUTPUT_DIR", "/tmp")
CLAUDE_ALLOWED_TOOLS = ["WebSearch"]

# --- Codex ---
CODEX_DEFAULT_MODEL = os.getenv("CODEX_MODEL", "gpt-5.5")
CODEX_DEFAULT_OUTPUT_DIR = os.getenv("CODEX_OUTPUT_DIR", "/tmp")

# --- Gemini (served via agy; the standalone gemini CLI was retired for consumer accounts) ---
# agy model alias — thinking level is baked into the name (see `agy models`). "(Low)" = low thinking.
GEMINI_DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "Gemini 3.5 Flash (Low)")
# Vertex grounding-redirect prefix — both the gemini and antigravity providers emit these;
# vertex_redirect.py follows them to the real source URLs.
VERTEX_REDIRECT_PREFIX = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/"

# --- Kimi ---
KIMI_DEFAULT_MODEL = os.getenv("KIMI_MODEL", "")
KIMI_DEFAULT_OUTPUT_DIR = os.getenv("KIMI_OUTPUT_DIR", "/tmp")
KIMI_SANDBOX_DIR = os.getenv("KIMI_SANDBOX_DIR", "/tmp/kimi-sandbox")

# --- Antigravity CLI (agy) — the agy integration that backs the `gemini` provider.
# OAuth-only; drives the host's Google AI subscription login. Not a separate provider. ---
ANTIGRAVITY_BINARY = os.getenv("ANTIGRAVITY_BINARY", "agy")
ANTIGRAVITY_DEFAULT_MODEL = os.getenv("ANTIGRAVITY_MODEL", "")
ANTIGRAVITY_DEFAULT_OUTPUT_DIR = os.getenv("ANTIGRAVITY_OUTPUT_DIR", "/tmp")
ANTIGRAVITY_SANDBOX_DIR = os.getenv("ANTIGRAVITY_SANDBOX_DIR", "/tmp/antigravity-sandbox")

# --- Grok (xAI) — headless `grok -p`; browser/OAuth subscription login stored in
# ~/.grok/auth.json (no API key). The grounding system prompt is injected verbatim via
# `--system-prompt-override` (true replacement, unlike `--rules` which only appends). ---
GROK_BINARY = os.getenv("GROK_BINARY", "grok")
GROK_DEFAULT_MODEL = os.getenv("GROK_MODEL", "grok-build")
GROK_DEFAULT_OUTPUT_DIR = os.getenv("GROK_OUTPUT_DIR", "/tmp")
GROK_SANDBOX_DIR = os.getenv("GROK_SANDBOX_DIR", "/tmp/grok-sandbox")
