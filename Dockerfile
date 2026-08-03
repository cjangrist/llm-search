FROM node:22-bookworm@sha256:7725a5c2c83eed1d36258c66efae14b1ceccd021db9ed1d9559d3335ed3d68ed AS antigravity-builder

ARG ANTIGRAVITY_VERSION=1.1.10
ARG ANTIGRAVITY_BUILD=6423386432339968
ARG ANTIGRAVITY_LINUX_AMD64_SHA512=e64d4e58ede0f8440f2b3dc021f9d6d36b05f5c2f74d5a9215c1f11b20d536c8c2e020f4ce5257aa67e940e94c94d5a16d3aa6461cda18ee7f3e74d3a20ca1ac
RUN curl -fsSL "https://storage.googleapis.com/antigravity-public/antigravity-cli/${ANTIGRAVITY_VERSION}-${ANTIGRAVITY_BUILD}/linux-x64/cli_linux_x64.tar.gz" -o /tmp/agy.tar.gz \
    && echo "${ANTIGRAVITY_LINUX_AMD64_SHA512}  /tmp/agy.tar.gz" | sha512sum -c - \
    && tar -xzf /tmp/agy.tar.gz -C /tmp antigravity

FROM node:22-bookworm@sha256:7725a5c2c83eed1d36258c66efae14b1ceccd021db9ed1d9559d3335ed3d68ed

# System deps (ripgrep is used by kimi-cli; avoids its slow auto-download)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv curl git ca-certificates gosu ripgrep unzip

# Install CLIs globally via npm (pinned to latest registry releases as of 2026-08-03).
# gemini-cli was removed: Google retired it for consumer accounts (IneligibleTierError) —
# the gemini provider is now served via the Antigravity CLI (agy), installed below.
ARG CLAUDE_CODE_VERSION=2.1.220
ARG CODEX_VERSION=0.146.0
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
ARG UV_VERSION=0.12.1
RUN curl -LsSf https://astral.sh/uv/${UV_VERSION}/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh

# Install Kimi CLI (MoonshotAI) via uv with its own managed Python,
# into world-readable paths so the llmsearch user can run it
ENV UV_TOOL_DIR=/opt/uv-tools
ENV UV_TOOL_BIN_DIR=/usr/local/bin
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
ARG KIMI_CLI_VERSION=1.49.0
RUN uv tool install "kimi-cli==${KIMI_CLI_VERSION}" --python 3.12 \
    && chmod -R a+rX /opt/uv-tools /opt/uv-python

# Install Antigravity CLI (agy) — OAuth-only; drives the host's Google AI subscription
# (no API key). Pin the release manifest version and checksum, then install
# system-wide so the non-root llmsearch user can run it.
COPY --from=antigravity-builder /tmp/antigravity /usr/local/bin/agy
RUN chmod 0755 /usr/local/bin/agy && agy --version

# Install Grok CLI (xAI) — browser/OAuth subscription login (no API key). The official
# installer pulls a pinned prebuilt static binary; install it under a world-readable /opt
# tree (HOME=/opt/grok) and symlink onto PATH (GROK_BIN_DIR) so the non-root llmsearch user
# can run it. We pin the version and disable the self-updater (ENV below + --no-auto-update).
ARG GROK_VERSION=0.2.118
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

# Entrypoint prepares writable, directly mounted credential directories.
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
