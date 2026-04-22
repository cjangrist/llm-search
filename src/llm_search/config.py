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
    "claude": {"model": "haiku", "timeout": 180},
    "codex": {"model": "gpt-5.4", "timeout": 180},
    "gemini": {"model": "gemini-3-flash-preview", "timeout": 180},
    "kimi": {"model": "", "timeout": 300},
}

# --- Claude ---
CLAUDE_DEFAULT_MODEL = os.getenv("CLAUDE_MODEL", "haiku")
CLAUDE_DEFAULT_OUTPUT_DIR = os.getenv("CLAUDE_OUTPUT_DIR", "/tmp")
CLAUDE_ALLOWED_TOOLS = ["WebSearch"]

# --- Codex ---
CODEX_DEFAULT_MODEL = os.getenv("CODEX_MODEL", "gpt-5.4")
CODEX_DEFAULT_OUTPUT_DIR = os.getenv("CODEX_OUTPUT_DIR", "/tmp")

# --- Gemini ---
GEMINI_DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
GEMINI_DEFAULT_OUTPUT_DIR = os.getenv("GEMINI_OUTPUT_DIR", "/tmp")
GEMINI_SANDBOX_DIR = os.getenv("GEMINI_SANDBOX_DIR", "/tmp/gemini-sandbox")
GEMINI_SCRIPT_PATH = os.getenv("GEMINI_SCRIPT_PATH", "")
VERTEX_REDIRECT_PREFIX = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/"

# --- Kimi ---
KIMI_DEFAULT_MODEL = os.getenv("KIMI_MODEL", "")
KIMI_DEFAULT_OUTPUT_DIR = os.getenv("KIMI_OUTPUT_DIR", "/tmp")
KIMI_SANDBOX_DIR = os.getenv("KIMI_SANDBOX_DIR", "/tmp/kimi-sandbox")
