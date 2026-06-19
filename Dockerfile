FROM node:22-bookworm@sha256:9059d9d7db987b86299e052ff6630cd95e5a770336967c21110e53289a877433

# System deps (ripgrep is used by kimi-cli; avoids its slow auto-download)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv curl git ca-certificates gosu ripgrep unzip \
    && rm -rf /var/lib/apt/lists/*

# Install CLIs globally via npm (pinned to match host versions as of 2026-06-03).
# gemini-cli was removed: Google retired it for consumer accounts (IneligibleTierError) —
# the gemini provider is now served via the Antigravity CLI (agy), installed below.
ARG CLAUDE_CODE_VERSION=2.1.162
ARG CODEX_VERSION=0.136.0
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    "@openai/codex@${CODEX_VERSION}"

# Python venv so pip doesn't complain about externally-managed
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Install Python dependencies (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install uv — pinned version (kimi-cli requires Python >=3.12 but base image ships 3.11)
ARG UV_VERSION=0.11.8
RUN curl -LsSf https://astral.sh/uv/${UV_VERSION}/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh

# Install Kimi CLI (MoonshotAI) via uv with its own managed Python,
# into world-readable paths so the llmsearch user can run it
ENV UV_TOOL_DIR=/opt/uv-tools
ENV UV_TOOL_BIN_DIR=/usr/local/bin
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
ARG KIMI_CLI_VERSION=1.46.0
RUN uv tool install "kimi-cli==${KIMI_CLI_VERSION}" --python 3.12 \
    && chmod -R a+rX /opt/uv-tools /opt/uv-python

# Install Antigravity CLI (agy) — OAuth-only; drives the host's Google AI subscription
# (no API key). The installer fetches the current release and self-updates in the
# background like on the host; install system-wide so the non-root llmsearch user can run it.
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash -s -- --dir /usr/local/bin \
    && agy --version

# Install the llm_search package
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir --no-deps .

# Copy operational scripts
COPY scripts/ scripts/

# Run as non-root with host UID so bind-mounted creds are readable
ARG HOST_UID=1000
RUN usermod -u 9999 node && \
    useradd -m -s /bin/bash -u ${HOST_UID} llmsearch

# Entrypoint copies mounted RO creds into user-owned dirs so CLIs can read them
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV LLM_SEARCH_OUTPUT_DIR=/tmp/llm-search
ENV LLM_SEARCH_PORT=8080
ENV NODE_COMPILE_CACHE=/tmp/node-compile-cache
ENV ANTIGRAVITY_SANDBOX_DIR=/tmp/antigravity-sandbox
ENV KIMI_SANDBOX_DIR=/tmp/kimi-sandbox

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--timeout", "330", "--workers", "4", "llm_search.server:app"]
