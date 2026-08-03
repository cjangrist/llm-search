#!/bin/bash
# Prepare directly mounted credential homes and drop privileges.

UHOME=/home/llmsearch

# Pre-create writable directories. These paths are direct bind mounts, so CLI
# logins and atomic token rotations persist on the host across container rebuilds.
for cli_dir in "$UHOME/.claude" "$UHOME/.codex" "$UHOME/.gemini" "$UHOME/.gemini/antigravity-cli" "$UHOME/.kimi" "$UHOME/.kimi/credentials" "$UHOME/.grok"; do
    mkdir -p "$cli_dir"
    chown llmsearch:llmsearch "$cli_dir"
done

# Ensure output and logs dirs are writable by llmsearch
mkdir -p /tmp/llm-search/logs
chown -R llmsearch:llmsearch /tmp/llm-search

# Scrub any secret-bearing artefacts left over from a crashed prior run before
# the new process can start, then retain the zeroed files in a traceable trash
# directory instead of deleting them.
secret_trash_directory="/tmp/llm-search/.trash/startup-$(date -u +%Y%m%dT%H%M%SZ)"
for stale_secret in /tmp/llm-search/codex_trace_*.log \
                    /tmp/llm-search/kimi_apiconfig_*.toml; do
    [ -f "$stale_secret" ] || continue
    size=$(stat -c%s "$stale_secret" 2>/dev/null || echo 0)
    if [ "$size" -gt 0 ]; then
        dd if=/dev/zero of="$stale_secret" bs=1 count="$size" conv=notrunc 2>/dev/null
        sync
    fi
    mkdir -p "$secret_trash_directory"
    mv "$stale_secret" "$secret_trash_directory/"
done

# Create the agy (Antigravity) sandbox parent dir. The antigravity provider runs agy
# with its cwd set to a per-request subdir of this, so agy operates in an isolated,
# writable workspace instead of the real filesystem. Per-request subdirs are created
# and cleaned up (moved to .trash) by the provider itself.
ANTIGRAVITY_SANDBOX_DIR="${ANTIGRAVITY_SANDBOX_DIR:-/tmp/antigravity-sandbox}"
mkdir -p "$ANTIGRAVITY_SANDBOX_DIR"
chown -R llmsearch:llmsearch "$ANTIGRAVITY_SANDBOX_DIR"

# Create empty sandbox dir for kimi to work from
KIMI_SANDBOX_DIR="${KIMI_SANDBOX_DIR:-/tmp/kimi-sandbox}"
mkdir -p "$KIMI_SANDBOX_DIR"
chown -R llmsearch:llmsearch "$KIMI_SANDBOX_DIR"

# Create the grok sandbox parent dir. The grok provider runs `grok -p` with its --cwd and
# per-request --leader-socket inside a subdir of this (absolute paths required), cleaned up
# (moved to .trash) by the provider after each request.
GROK_SANDBOX_DIR="${GROK_SANDBOX_DIR:-/tmp/grok-sandbox}"
mkdir -p "$GROK_SANDBOX_DIR"
chown -R llmsearch:llmsearch "$GROK_SANDBOX_DIR"

# Warm up Node.js module caches in background (don't block startup)
(gosu llmsearch timeout 15 claude --version >/dev/null 2>&1;
 gosu llmsearch timeout 15 codex --version >/dev/null 2>&1;
 gosu llmsearch timeout 15 kimi --version >/dev/null 2>&1;
 gosu llmsearch timeout 15 agy --version >/dev/null 2>&1;
 gosu llmsearch timeout 15 grok --version >/dev/null 2>&1) &

# Drop privileges and run CMD
exec gosu llmsearch "$@"
