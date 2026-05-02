#!/bin/bash
# Copy RO-mounted creds into user-owned dirs so CLIs can read them.
# Background loop re-syncs every 30s using smart expiry-aware sync
# that avoids overwriting fresher container tokens with stale host ones.

UHOME=/home/llmsearch

smart_sync() {
    python3 /app/scripts/sync_creds.py
    # Fix ownership on any newly synced files
    chown -R llmsearch:llmsearch "$UHOME/.claude" "$UHOME/.codex" "$UHOME/.gemini" "$UHOME/.kimi" 2>/dev/null
    # Re-apply gemini settings overrides (sync_creds may overwrite settings.json)
    python3 /app/scripts/configure_gemini_settings.py "$UHOME"
    chown llmsearch:llmsearch "$UHOME/.gemini/settings.json" 2>/dev/null
}

# Pre-create writable dirs
for cli_dir in "$UHOME/.claude" "$UHOME/.codex" "$UHOME/.gemini" "$UHOME/.kimi" "$UHOME/.kimi/credentials"; do
    mkdir -p "$cli_dir"
    chown llmsearch:llmsearch "$cli_dir"
done

# Symlink system ripgrep so gemini-cli doesn't try to download it
mkdir -p "$UHOME/.gemini/tmp/bin"
ln -sf "$(which rg)" "$UHOME/.gemini/tmp/bin/rg"
chown -R llmsearch:llmsearch "$UHOME/.gemini/tmp"

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

# Create empty sandbox dir for gemini to work from (nothing to scan)
GEMINI_SANDBOX_DIR="${GEMINI_SANDBOX_DIR:-/tmp/gemini-sandbox}"
mkdir -p "$GEMINI_SANDBOX_DIR"
echo '*' > "$GEMINI_SANDBOX_DIR/.geminiignore"
chown -R llmsearch:llmsearch "$GEMINI_SANDBOX_DIR"

# Create empty sandbox dir for kimi to work from
KIMI_SANDBOX_DIR="${KIMI_SANDBOX_DIR:-/tmp/kimi-sandbox}"
mkdir -p "$KIMI_SANDBOX_DIR"
chown -R llmsearch:llmsearch "$KIMI_SANDBOX_DIR"

# Initial sync (includes settings override)
smart_sync

# Background refresh every 30s
(while true; do sleep 30; smart_sync; done) &

# Warm up Node.js module caches in background (don't block startup)
(gosu llmsearch timeout 15 claude --version >/dev/null 2>&1;
 gosu llmsearch timeout 15 codex --version >/dev/null 2>&1;
 gosu llmsearch timeout 15 gemini --version >/dev/null 2>&1;
 gosu llmsearch timeout 15 kimi --version >/dev/null 2>&1) &

# Drop privileges and run CMD
exec gosu llmsearch "$@"
