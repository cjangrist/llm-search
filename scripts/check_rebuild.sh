#!/bin/bash
# Check if npm packages have new versions and rebuild the Docker image if so.
# Run via cron: 0 */6 * * * /path/to/llm-search/scripts/check_rebuild.sh
#
# Stores last-known versions in .package-versions. If any differ, triggers rebuild.

set -euo pipefail

SCRIPT_DIRECTORY="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIRECTORY="$(cd "$SCRIPT_DIRECTORY/.." && pwd)"
VERSION_FILE="$PROJECT_DIRECTORY/.package-versions"
LOG_FILE="$PROJECT_DIRECTORY/.rebuild.log"

PACKAGES=(
    "@anthropic-ai/claude-code"
    "@openai/codex"
    "@google/gemini-cli"
)

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG_FILE"
}

get_latest_versions() {
    local versions=""
    for package_name in "${PACKAGES[@]}"; do
        latest_version=$(npm view "$package_name" version 2>/dev/null || echo "unknown")
        versions+="$package_name=$latest_version"$'\n'
    done
    echo "$versions"
}

current_versions=$(get_latest_versions)

if [ -f "$VERSION_FILE" ] && [ "$(cat "$VERSION_FILE")" = "$current_versions" ]; then
    log "No package updates detected"
    exit 0
fi

log "Package update detected, rebuilding..."
log "New versions: $(echo "$current_versions" | tr '\n' ' ')"

cd "$PROJECT_DIRECTORY"
docker compose build --no-cache >> "$LOG_FILE" 2>&1
docker compose up -d >> "$LOG_FILE" 2>&1

echo "$current_versions" > "$VERSION_FILE"
log "Rebuild complete, container restarted"
