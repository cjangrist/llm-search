# LLM Search

OpenAI-compatible Chat Completions API that wraps three LLM CLI providers — **Claude Code**, **Codex**, and **Gemini CLI** — to perform web-search-grounded queries. Each provider's CLI is invoked non-interactively inside a Docker container, and the results are normalized into the OpenAI Chat Completions response format with `url_citation` annotations.

The CLIs are used instead of direct API calls to comply with each provider's Terms of Service — the CLI tools include built-in web search capabilities not equivalently available through their raw APIs.

## Quick Start

```bash
# Build and run
doppler run -- docker compose up -d --build

# Test
curl -X POST http://localhost:8041/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "gemini/search-fast", "messages": [{"role": "user", "content": "What is the current price of Bitcoin?"}]}'
```

## API

### POST /v1/chat/completions

OpenAI-compatible Chat Completions endpoint with web search grounding.

**Request:**
```json
{
  "model": "provider/model_name",
  "messages": [{"role": "user", "content": "your query"}],
  "timeout": 180
}
```

| Provider | Example Model | Notes |
|----------|--------------|-------|
| `claude` | `claude/haiku` | Claude Code CLI with WebSearch tool |
| `codex` | `codex/gpt-5.4` | Codex CLI with web search |
| `gemini` | `gemini/search-fast` | Gemini CLI with google_web_search |

**Response:** Standard OpenAI Chat Completions format with `url_citation` annotations on `choices[0].message`.

### GET /health

Returns `{"status": "ok"}`.

### GET /providers

Lists available providers with default models and timeouts.

## Architecture

```
HTTP POST /v1/chat/completions
    │
    ├── claude.py  → Claude Code CLI  → stream-json parsing
    ├── codex.py   → Codex CLI        → JSONL + SSE trace parsing
    └── gemini.py  → Gemini CLI       → activity log + grounding metadata
    │
    ▼
OpenAI Chat Completions JSON with url_citation annotations
```

## Project Structure

```
├── src/llm_search/              Python package
│   ├── server.py                Flask API server
│   ├── config.py                Environment-based configuration
│   ├── response.py              OpenAI response format builder
│   ├── logging_setup.py         Colorized logging
│   ├── prompts/                 System prompt
│   └── providers/               CLI integrations (claude, codex, gemini)
├── scripts/                     Operational scripts
│   ├── sync_creds.py            Credential sync between host and container
│   ├── configure_gemini_settings.py
│   └── check_rebuild.sh         Auto-rebuild on npm updates
├── docker/                      Docker entrypoint
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

## Configuration

All settings are loaded from environment variables. Copy `.env.example` to `.env` for local development.

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_SEARCH_OUTPUT_DIR` | `/tmp/llm-search` | Directory for intermediate JSON output |
| `LLM_SEARCH_PORT` | `8080` | API listen port |
| `LLM_SEARCH_HOST` | `0.0.0.0` | API bind address |
| `CLAUDE_MODEL` | `haiku` | Default Claude model |
| `CODEX_MODEL` | `gpt-5.4` | Default Codex model |
| `GEMINI_MODEL` | `search-fast` | Default Gemini model |

## Prerequisites

- Docker and Docker Compose
- CLI credentials for at least one provider:
  - Claude: `~/.claude/.credentials.json` (via `claude auth login`)
  - Codex: `~/.codex/auth.json` (via `codex auth login`)
  - Gemini: `~/.gemini/oauth_creds.json` (via `gemini auth login`)

## Development

```bash
# Install in development mode
pip install -e .

# Run the development server
python -m llm_search --port 8080

# Run a single provider standalone
python -m llm_search.providers.claude "What is quantum computing?" -m haiku -v
python -m llm_search.providers.codex "Latest news on AI" -m gpt-5.4 -v
python -m llm_search.providers.gemini "Bitcoin price today" -m search-fast -v
```

## License

MIT
