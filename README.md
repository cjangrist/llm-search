# LLM Search

[![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python)](https://python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.0+-green?logo=flask)](https://flask.palletsprojects.com/)
[![License](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-ready-blue?logo=docker)](https://docker.com/)

> **OpenAI-compatible Chat Completions API with web search grounding via Claude Code, Codex, Kimi, and Gemini CLIs.**

LLM Search provides a unified interface to several leading LLM CLI providers—**Claude Code**, **OpenAI Codex**, **Kimi** (MoonshotAI), and **Gemini**—each with built-in web search capabilities. Instead of calling raw APIs that lack search, we leverage the CLIs' Terms-of-Service-compliant search tools to deliver grounded, cited answers through a standard OpenAI-compatible interface.

> **Note:** the `gemini` provider is served through Google's **Antigravity CLI** (`agy`). Google retired the standalone `gemini-cli` for consumer accounts, so `gemini/*` requests run through `agy` (OAuth, your Google AI subscription) pinned to a Gemini model — existing clients keep working unchanged.

---

## 🚀 Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/yourusername/llm-search.git
cd llm-search

# 2. Set up CLI credentials (one-time)
claude          # Claude Code — sign in
codex auth login
kimi login      # or set KIMI_API_KEY
agy             # Gemini — sign in with your Google account (OAuth); powers the gemini provider

# 3. Build and run (HOST_UID matches container user to your host for cred mounts)
HOST_UID=$(id -u) docker compose up -d --build

# 4. Test the API
curl -X POST http://localhost:8041/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "gemini", "messages": [{"role": "user", "content": "What is the current price of Bitcoin?"}]}'
```

---

## 🔧 Configuration

### Prerequisites

You need CLI credentials for at least one provider:

| Provider | Credential File | Setup Command |
|----------|----------------|---------------|
| **Claude** | `~/.claude/.credentials.json` | `claude` |
| **Codex** | `~/.codex/auth.json` | `codex auth login` |
| **Kimi** | `~/.kimi/credentials/kimi-code.json` | `kimi login` (or `KIMI_API_KEY`) |
| **Gemini** (via `agy`) | `~/.gemini/antigravity-cli/antigravity-oauth-token` | `agy` (Google OAuth) |

These credentials are automatically mounted into the Docker container and synced every 30 seconds.
The Gemini provider runs through Google's Antigravity CLI (`agy`), which is **OAuth-only** — it drives your Google AI subscription login; no API key is used.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_SEARCH_OUTPUT_DIR` | `/tmp/llm-search` | Directory for intermediate JSON output |
| `LLM_SEARCH_PORT` | `8080` | API listen port (inside container) |
| `LLM_SEARCH_HOST` | `0.0.0.0` | API bind address |
| `CLAUDE_MODEL` | `haiku` | Default Claude model |
| `CODEX_MODEL` | `gpt-5.5` | Default Codex model |
| `GEMINI_MODEL` | `Gemini 3.5 Flash (Low)` | agy model alias the `gemini` provider is pinned to (thinking level is baked into the name; see `agy models`) |

### Docker Compose Port Mapping

The default `docker-compose.yml` exposes the service on **port 8041**:

```yaml
services:
  llm-search:
    build: .
    ports:
      - "8041:8080"  # Host:Container
```

---

## 🛠️ Usage

### OpenAI-Compatible Endpoint

The main endpoint follows the OpenAI Chat Completions API specification:

```bash
curl -X POST http://localhost:8041/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer any-key" \
  -d '{
    "model": "provider/model_name",
    "messages": [{"role": "user", "content": "Your search query here"}],
    "timeout": 180
  }'
```

**Model format:** `provider/model_name`

| Provider | Example Model | Description |
|----------|--------------|-------------|
| `claude` | `claude/haiku` | Claude Code with WebSearch tool |
| `codex` | `codex/gpt-5.5` | OpenAI Codex with web search |
| `kimi` | `kimi` | Kimi (MoonshotAI) with web search |
| `gemini` | `gemini` | Google web search via the Antigravity CLI (`agy`) on your subscription OAuth; pinned to a Gemini model (the model field is ignored) |

**Response with citations:**

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1712345678,
  "model": "gemini",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "Bitcoin is currently trading at $67,234 [Coinbase](https://coinbase.com)...",
      "annotations": [
        {
          "type": "url_citation",
          "url_citation": {
            "start_index": 45,
            "end_index": 78,
            "url": "https://coinbase.com",
            "title": "Coinbase"
          }
        }
      ]
    },
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0
  }
}
```

### Health Check

```bash
curl http://localhost:8041/health
# {"status": "ok"}
```

### List Available Providers

```bash
curl http://localhost:8041/providers
# {
#   "claude": {"default_model": "haiku", "default_timeout": 290},
#   "codex": {"default_model": "gpt-5.5", "default_timeout": 290},
#   "kimi": {"default_model": "", "default_timeout": 290},
#   "gemini": {"default_model": "Gemini 3.5 Flash (Low)", "default_timeout": 290}
# }
```

### Standalone Provider Usage

Each provider can be run independently for testing:

```bash
# Claude Code
python -m llm_search.providers.claude "What is quantum computing?" -m haiku -v

# Codex
python -m llm_search.providers.codex "Latest AI news" -m gpt-5.5 -v

# Kimi
python -m llm_search.providers.kimi "Bitcoin price today" -v

# Gemini (the agy integration; standalone debug entry point)
python -m llm_search.providers.antigravity "Bitcoin price today" -m "Gemini 3.5 Flash (Low)" -v
```

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    HTTP Request Handler                         │
│  ┌──────────────┐  ┌──────────────────┐  ┌──────────────┐      │
│  │   /health    │  │ /v1/chat/completions│  │ /providers   │      │
│  └──────┬───────┘  └────────┬─────────┘  └──────┬───────┘      │
│         │                   │                    │              │
│         └───────────────────┼────────────────────┘              │
│                             ▼                                   │
│                ┌─────────────────────────┐                     │
│                │   Model Parser          │                     │
│                │  (provider/model_name)  │                     │
│                └───────────┬─────────────┘                     │
│                            ▼                                    │
│              ┌─────────────────────────────┐                   │
│              │    Provider Dispatcher      │                   │
│  ┌───────────┴──────────┬──────────────────┴──────────┐       │
│  │                      │                              │       │
│  ▼                      ▼                              ▼       │
│ ┌─────────┐       ┌─────────┐                  ┌─────────┐    │
│ │ Claude  │       │  Codex  │                  │   agy   │    │
│ │  CLI    │       │   CLI   │                  │   CLI   │    │
│ │         │       │         │                  │         │    │
│ │ stream- │       │ JSONL + │                  │ activity│    │
│ │  json   │       │  SSE    │                  │  log    │    │
│ └────┬────┘       └────┬────┘                  └────┬────┘    │
│      │                 │                             │         │
│      └─────────────────┼─────────────────────────────┘         │
│                        ▼                                       │
│           ┌──────────────────────┐                            │
│           │  Response Builder    │                            │
│           │  (OpenAI format)     │                            │
│           │  + url_citations     │                            │
│           └──────────┬───────────┘                            │
│                      ▼                                         │
│           ┌──────────────────────┐                            │
│           │  JSON Response       │                            │
│           │  with annotations    │                            │
│           └──────────────────────┘                            │
└─────────────────────────────────────────────────────────────────┘
```

### Key Design Decisions

1. **CLI-First Architecture**
   - Uses official CLIs instead of APIs to access web search capabilities
   - Complies with each provider's Terms of Service
   - CLIs include built-in rate limiting and retry logic

2. **Credential Synchronization**
   - Background sync every 30 seconds via [`sync_creds.py`](scripts/sync_creds.py:1)
   - Smart expiry-aware logic prevents overwriting fresh tokens with stale ones
   - Extracts and compares JWT `exp` claims, OAuth expiry dates

3. **Provider-Specific Parsing**
   - **Claude**: Parses `stream-json` format, extracts WebSearch tool results
   - **Codex**: Captures `RUST_LOG=trace` SSE events + JSONL fallback
   - **Gemini** (via `agy`): runs `agy --print` under a PTY, parses markdown citations, resolves Vertex AI redirect URIs
   - **Kimi**: Parses `stream-json` events, extracts web-search sources

4. **OpenAI Compatibility**
   - Full Chat Completions API spec compliance
   - `url_citation` annotations in standard format
   - Works with existing OpenAI client libraries

5. **Non-Root Container Execution**
   - Runs as `llmsearch` user with host UID mapping
   - Credential files copied from read-only mounts to user-owned directories
   - Secure sandbox for CLI execution

---

## 📁 Project Structure

```
├── src/llm_search/              # Python package
│   ├── __init__.py              # Package version
│   ├── __main__.py              # CLI entry point
│   ├── server.py                # Flask API server
│   ├── config.py                # Environment configuration
│   ├── response.py              # OpenAI response builder
│   ├── logging_setup.py         # Colorized logging
│   ├── prompts/                 # System prompts
│   │   ├── __init__.py          # Prompt loader
│   │   └── system_prompt.md     # Shared system prompt
│   └── providers/               # CLI integrations
│       ├── __init__.py          # Provider registry
│       ├── claude.py            # Claude Code integration
│       ├── codex.py             # Codex integration
│       ├── kimi.py              # Kimi (MoonshotAI) integration
│       ├── kimi_parsing.py      # Kimi stream-json parsing
│       ├── antigravity.py       # Antigravity CLI (agy) integration — backs the gemini provider
│       ├── vertex_redirect.py   # Shared Vertex grounding-redirect resolver
│       └── subprocess_safety.py # Subprocess-tree cleanup + env sanitization
├── scripts/                     # Operational scripts
│   ├── sync_creds.py            # Smart credential sync
│   ├── integration_test.py      # API integration tests
│   └── check_rebuild.sh         # Auto-rebuild on updates
├── docker/
│   └── entrypoint.sh            # Container entrypoint
├── Dockerfile                   # Multi-stage container build
├── docker-compose.yml           # Compose with credential mounts
├── pyproject.toml               # Package configuration
├── requirements.txt             # Python dependencies
└── README.md                    # This file
```

---

## 🔌 Adding a New Provider

The codebase uses a registry pattern for easy extensibility:

1. **Create provider module** (`src/llm_search/providers/newprovider.py`):

```python
"""NewProvider CLI integration."""
import logging
from datetime import datetime
import sh
from llm_search.prompts import load_system_prompt

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "newprovider-model"

def call_newprovider(prompt, model, timeout_seconds):
    """Call NewProvider CLI and return raw output."""
    system_prompt = load_system_prompt()
    result = sh.newprovider_cli(
        "--query", prompt,
        "--model", model,
        "--system", system_prompt,
        _timeout=timeout_seconds,
    )
    return str(result)

def parse_results(raw_text):
    """Parse CLI output into search queries and sources."""
    # Implementation here
    return [], []

def run_search(prompt, model, output_dir, timeout):
    """Run NewProvider search and return OpenAI-format result."""
    raw_text = call_newprovider(prompt, model, timeout)
    search_queries, sources = parse_results(raw_text)
    model_response = extract_response(raw_text)
    
    # Build OpenAI format output
    output = build_openai_format(search_queries, sources, model_response)
    return output, model_response
```

2. **Register in provider registry** (`src/llm_search/providers/__init__.py`):

```python
from llm_search.providers import antigravity, claude, codex, kimi, newprovider

PROVIDER_RUNNERS = {
    "claude": claude.run_search,
    "codex": codex.run_search,
    "kimi": kimi.run_search,
    "gemini": antigravity.run_search_as_gemini,  # gemini is served via agy
    "newprovider": newprovider.run_search,  # Add here
}
```

3. **Add configuration** (`src/llm_search/config.py`):

```python
PROVIDER_DEFAULTS = {
    "claude": {"model": "haiku", "timeout": 290},
    "codex": {"model": "gpt-5.5", "timeout": 290},
    "kimi": {"model": "", "timeout": 290},
    "gemini": {"model": "Gemini 3.5 Flash (Low)", "timeout": 290},
    "newprovider": {"model": "default-model", "timeout": 290},
}
```

4. **Update Docker** (`docker-compose.yml`):

```yaml
volumes:
  - ~/.newprovider/creds.json:/mnt/creds/newprovider/creds.json:ro
```

No other changes needed. The server will automatically expose the new provider at `/providers` and route requests to it.

---

## 🧪 Development

### Local Installation

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install in development mode
pip install -e .

# Run development server
python -m llm_search --port 8080
```

### Integration Testing

```bash
# Run full integration test suite
python scripts/integration_test.py

# Test specific providers
python scripts/integration_test.py --providers claude gemini

# Test against custom endpoint
python scripts/integration_test.py --base-url http://localhost:8080
```

### Building Docker Image

```bash
# Build with custom UID (match your host user)
docker build --build-arg HOST_UID=$(id -u) -t llm-search .

# Run with Doppler for secret management
doppler run -- docker compose up -d --build
```

### Auto-Rebuild on CLI Updates

Set up a cron job to automatically rebuild when new CLI versions are released:

```bash
# Add to crontab (checks every 6 hours)
0 */6 * * * /path/to/llm-search/scripts/check_rebuild.sh
```

---

## 🐛 Troubleshooting

### Container fails to start

```
Error: Credential file not found
```

**Fix:** Ensure you've logged into at least one CLI on the host:
```bash
claude    # or codex, kimi, or agy (for Gemini)
docker compose up -d --build
```

### Provider returns empty response

**Check logs:**
```bash
docker logs llm-search-llm-search-1
```

**Common causes:**
- CLI credentials expired (re-run `auth login`)
- Rate limiting (wait and retry)
- Network connectivity inside container

### Slow responses

- Default timeout is 180 seconds for complex queries
- Reduce with `"timeout": 60` in request body
- Check provider status pages for outages

### Permission errors

```
PermissionError: [Errno 13] Permission denied
```

**Fix:** The container runs with your host UID. Rebuild with correct UID:
```bash
docker compose down
docker build --build-arg HOST_UID=$(id -u) --no-cache .
docker compose up -d
```

### Credential sync issues

**Check sync logs:**
```bash
docker exec llm-search-llm-search-1 tail -f /proc/1/fd/2
```

**Manual sync test:**
```bash
docker exec -it llm-search-llm-search-1 python3 /app/scripts/sync_creds.py
```

---

## 📄 License

MIT License — see [LICENSE](LICENSE:1) for details.

---

## 🤝 Contributing

Contributions welcome! Areas for improvement:

- Additional LLM providers (z.ai, kimi, etc.)
- Streaming response support
- Metrics and observability

Please open an issue or PR with your ideas.
