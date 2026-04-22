FROM node:22-bookworm

# System deps (includes ripgrep to skip gemini-cli's slow auto-download)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv curl git ca-certificates gosu ripgrep unzip \
    && rm -rf /var/lib/apt/lists/*

# Install Bun for faster gemini CLI runtime (~2x node startup speedup)
RUN curl -fsSL https://bun.sh/install | BUN_INSTALL=/usr/local bash

# Install CLIs globally via npm
RUN npm install -g @anthropic-ai/claude-code@latest \
    @openai/codex@latest \
    @google/gemini-cli@preview

# Python venv so pip doesn't complain about externally-managed
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Install Python dependencies (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install uv (needed because kimi-cli requires Python >=3.12 but base image ships 3.11)
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh

# Install Kimi CLI (MoonshotAI) via uv with its own managed Python,
# into world-readable paths so the llmsearch user can run it
ENV UV_TOOL_DIR=/opt/uv-tools
ENV UV_TOOL_BIN_DIR=/usr/local/bin
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
RUN uv tool install kimi-cli --python 3.12 \
    && chmod -R a+rX /opt/uv-tools /opt/uv-python

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
ENV GEMINI_SANDBOX_DIR=/tmp/gemini-sandbox
ENV KIMI_SANDBOX_DIR=/tmp/kimi-sandbox

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--timeout", "300", "--workers", "4", "llm_search.server:app"]
