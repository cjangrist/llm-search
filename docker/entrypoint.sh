#!/bin/bash
# Copy RO-mounted creds into user-owned dirs so CLIs can read them.
# Background loop re-syncs every 30s using smart expiry-aware sync
# that avoids overwriting fresher container tokens with stale host ones.

UHOME=/home/llmsearch

smart_sync() {
    python3 /app/scripts/sync_creds.py
    # Fix ownership on any newly synced files
    chown -R llmsearch:llmsearch "$UHOME/.claude" "$UHOME/.codex" "$UHOME/.gemini" 2>/dev/null
    # Re-apply gemini settings overrides (sync_creds may overwrite settings.json)
    python3 /app/scripts/configure_gemini_settings.py "$UHOME"
    chown llmsearch:llmsearch "$UHOME/.gemini/settings.json" 2>/dev/null
}

# Pre-create writable dirs
for cli_dir in "$UHOME/.claude" "$UHOME/.codex" "$UHOME/.gemini"; do
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

# Create empty sandbox dir for gemini to work from (nothing to scan)
GEMINI_SANDBOX_DIR="${GEMINI_SANDBOX_DIR:-/tmp/gemini-sandbox}"
mkdir -p "$GEMINI_SANDBOX_DIR"
echo '*' > "$GEMINI_SANDBOX_DIR/.geminiignore"
chown -R llmsearch:llmsearch "$GEMINI_SANDBOX_DIR"

# Initial sync (includes settings override)
smart_sync

# Background refresh every 30s
(while true; do sleep 30; smart_sync; done) &

# Warm up Node.js module caches in background (don't block startup)
(gosu llmsearch timeout 15 claude --version >/dev/null 2>&1;
 gosu llmsearch timeout 15 codex --version >/dev/null 2>&1;
 gosu llmsearch timeout 15 gemini --version >/dev/null 2>&1) &

# Drop privileges and run CMD
exec gosu llmsearch "$@"
