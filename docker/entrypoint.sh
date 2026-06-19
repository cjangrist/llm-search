#!/bin/bash
# Copy RO-mounted creds into user-owned dirs so CLIs can read them.
# Background loop re-syncs every 30s using smart expiry-aware sync
# that avoids overwriting fresher container tokens with stale host ones.

UHOME=/home/llmsearch

smart_sync() {
    python3 /app/scripts/sync_creds.py
    # Fix ownership on any newly synced files. ~/.gemini holds agy's antigravity-cli
    # app-data + OAuth token; the others are claude/codex/kimi creds.
    chown -R llmsearch:llmsearch "$UHOME/.claude" "$UHOME/.codex" "$UHOME/.gemini" "$UHOME/.kimi" 2>/dev/null
}

# Pre-create writable dirs ($UHOME/.gemini/antigravity-cli is agy's app-data + OAuth token dir)
for cli_dir in "$UHOME/.claude" "$UHOME/.codex" "$UHOME/.gemini" "$UHOME/.gemini/antigravity-cli" "$UHOME/.kimi" "$UHOME/.kimi/credentials"; do
    mkdir -p "$cli_dir"
    chown llmsearch:llmsearch "$cli_dir"
done

# Ensure output and logs dirs are writable by llmsearch
mkdir -p /tmp/llm-search/logs
chown -R llmsearch:llmsearch /tmp/llm-search

# Scrub any secret-bearing artefacts left over from a crashed prior run before
# the new process can start. Codex trace logs embed `Authorization: Bearer …`
# (RUST_LOG=trace), kimi config TOMLs embed the literal API key. Overwrite-then-
# unlink is the secure-delete idiom (RULE_07's no-rm rule covers user data, not
# secrets). 2>/dev/null absorbs the empty-glob case on a fresh container.
for stale_secret in /tmp/llm-search/codex_trace_*.log \
                    /tmp/llm-search/kimi_apiconfig_*.toml \
                    /tmp/llm-search/.trash/*/codex_trace_*.log \
                    /tmp/llm-search/.trash/*/kimi_apiconfig_*.toml; do
    [ -f "$stale_secret" ] || continue
    size=$(stat -c%s "$stale_secret" 2>/dev/null || echo 0)
    if [ "$size" -gt 0 ]; then
        dd if=/dev/zero of="$stale_secret" bs=1 count="$size" conv=notrunc 2>/dev/null
        sync
    fi
    rm -f "$stale_secret" 2>/dev/null
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

# Initial sync
smart_sync

# Background refresh every 30s
(while true; do sleep 30; smart_sync; done) &

# Warm up Node.js module caches in background (don't block startup)
(gosu llmsearch timeout 15 claude --version >/dev/null 2>&1;
 gosu llmsearch timeout 15 codex --version >/dev/null 2>&1;
 gosu llmsearch timeout 15 kimi --version >/dev/null 2>&1;
 gosu llmsearch timeout 15 agy --version >/dev/null 2>&1) &

# Drop privileges and run CMD
exec gosu llmsearch "$@"
