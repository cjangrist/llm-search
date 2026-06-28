FROM node:22-bookworm@sha256:c601a46abb4d2ab80a9dc3da208d50d1122642d53f17a101926ace71e5a9bf1c

# System deps (ripgrep is used by kimi-cli; avoids its slow auto-download)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv curl git ca-certificates gosu ripgrep unzip \
    && rm -rf /var/lib/apt/lists/*

# Install CLIs globally via npm (pinned to latest registry releases as of 2026-06-28).
# gemini-cli was removed: Google retired it for consumer accounts (IneligibleTierError) —
# the gemini provider is now served via the Antigravity CLI (agy), installed below.
ARG CLAUDE_CODE_VERSION=2.1.195
ARG CODEX_VERSION=0.142.3
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
ARG UV_VERSION=0.11.25
RUN curl -LsSf https://astral.sh/uv/${UV_VERSION}/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh

# Install Kimi CLI (MoonshotAI) via uv with its own managed Python,
# into world-readable paths so the llmsearch user can run it
ENV UV_TOOL_DIR=/opt/uv-tools
ENV UV_TOOL_BIN_DIR=/usr/local/bin
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
ARG KIMI_CLI_VERSION=1.48.0
RUN uv tool install "kimi-cli==${KIMI_CLI_VERSION}" --python 3.12 \
    && chmod -R a+rX /opt/uv-tools /opt/uv-python

# Install Antigravity CLI (agy) — OAuth-only; drives the host's Google AI subscription
# (no API key). Pin the release manifest version and checksum, then install
# system-wide so the non-root llmsearch user can run it.
ARG ANTIGRAVITY_VERSION=1.0.13
ARG ANTIGRAVITY_LINUX_AMD64_SHA512=f8be088ceb90e77503b04039eb8657f1ffac29bab37f9058c2587faf364105900e7b72fe9311744c83fb19f6f9f0b2036b63bc01c7a3fff7a6abfe9c02164a6f
RUN curl -fsSL https://storage.googleapis.com/antigravity-public/antigravity-cli/${ANTIGRAVITY_VERSION}-5758107482193920/linux-x64/cli_linux_x64.tar.gz -o /tmp/agy.tar.gz \
    && echo "${ANTIGRAVITY_LINUX_AMD64_SHA512}  /tmp/agy.tar.gz" | sha512sum -c - \
    && tar -xzf /tmp/agy.tar.gz -C /tmp antigravity \
    && install -m 0755 /tmp/antigravity /usr/local/bin/agy \
    && rm /tmp/agy.tar.gz /tmp/antigravity \
    && agy --version

# Install Grok CLI (xAI) — browser/OAuth subscription login (no API key). The official
# installer pulls a pinned prebuilt static binary; install it under a world-readable /opt
# tree (HOME=/opt/grok) and symlink onto PATH (GROK_BIN_DIR) so the non-root llmsearch user
# can run it. We pin the version and disable the self-updater (ENV below + --no-auto-update).
ARG GROK_VERSION=0.2.72
RUN HOME=/opt/grok GROK_BIN_DIR=/usr/local/bin \
      bash -c "curl -fsSL https://x.ai/cli/install.sh | bash -s ${GROK_VERSION}" \
    && chmod -R a+rX /opt/grok \
    && grok --version

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
ENV GROK_SANDBOX_DIR=/tmp/grok-sandbox
ENV GROK_DISABLE_AUTOUPDATER=1

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--timeout", "330", "--workers", "4", "llm_search.server:app"]
