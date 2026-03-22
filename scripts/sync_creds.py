#!/usr/bin/env python3
"""Smart credential sync: only overwrites container tokens when host tokens are fresher.

Compares token expiry timestamps before syncing to prevent the background
sync loop from clobbering tokens that the container CLI has already refreshed.
Config-only files (no tokens) are always synced when different.

Usage: python3 sync_creds.py  (called from entrypoint.sh every 30s)
"""
import base64
import json
import os
import shutil
import sys
import time


CONTAINER_HOME = os.getenv("SYNC_TARGET_HOME", "/home/llmsearch")

CREDENTIAL_PAIRS = [
    ("/mnt/creds/claude/.credentials.json", f"{CONTAINER_HOME}/.claude/.credentials.json"),
    ("/mnt/creds/codex/auth.json", f"{CONTAINER_HOME}/.codex/auth.json"),
    ("/mnt/creds/codex/config.toml", f"{CONTAINER_HOME}/.codex/config.toml"),
    ("/mnt/creds/gemini/oauth_creds.json", f"{CONTAINER_HOME}/.gemini/oauth_creds.json"),
    ("/mnt/creds/gemini/google_accounts.json", f"{CONTAINER_HOME}/.gemini/google_accounts.json"),
    ("/mnt/creds/gemini/settings.json", f"{CONTAINER_HOME}/.gemini/settings.json"),
]

TOKEN_FILES = {
    ".credentials.json",
    "auth.json",
    "oauth_creds.json",
}


def log(message):
    print(f"[sync_creds] {message}", file=sys.stderr, flush=True)


def files_identical(path_a, path_b):
    try:
        with open(path_a, "rb") as file_a, open(path_b, "rb") as file_b:
            return file_a.read() == file_b.read()
    except OSError:
        return False


def extract_claude_expiry(filepath):
    """Extract expiresAt (ms epoch) from Claude .credentials.json."""
    try:
        with open(filepath) as credential_file:
            data = json.load(credential_file)
        return data.get("claudeAiOauth", {}).get("expiresAt", 0)
    except (json.JSONDecodeError, OSError, KeyError):
        return 0


def extract_codex_expiry(filepath):
    """Extract access_token JWT exp (converted to ms) from Codex auth.json."""
    try:
        with open(filepath) as credential_file:
            data = json.load(credential_file)
        access_token = data.get("tokens", {}).get("access_token", "")
        if not access_token:
            return 0
        jwt_parts = access_token.split(".")
        if len(jwt_parts) < 2:
            return 0
        padded_payload = jwt_parts[1] + "=" * (4 - len(jwt_parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded_payload))
        return payload.get("exp", 0) * 1000
    except (json.JSONDecodeError, OSError, KeyError, ValueError):
        return 0


def extract_gemini_expiry(filepath):
    """Extract expiry_date (ms epoch) from Gemini oauth_creds.json."""
    try:
        with open(filepath) as credential_file:
            data = json.load(credential_file)
        return data.get("expiry_date", 0)
    except (json.JSONDecodeError, OSError, KeyError):
        return 0


EXPIRY_EXTRACTORS = {
    ".credentials.json": extract_claude_expiry,
    "auth.json": extract_codex_expiry,
    "oauth_creds.json": extract_gemini_expiry,
}


def is_token_file(filepath):
    return os.path.basename(filepath) in TOKEN_FILES


def host_token_is_fresher(host_path, container_path):
    """Return True if the host token has a later expiry than the container token."""
    basename = os.path.basename(container_path)
    extractor = EXPIRY_EXTRACTORS.get(basename)
    if extractor is None:
        return True

    host_expiry = extractor(host_path)
    container_expiry = extractor(container_path)

    if host_expiry == 0 and container_expiry == 0:
        return True

    if host_expiry > container_expiry:
        return True
    elif host_expiry == container_expiry:
        return not files_identical(host_path, container_path)
    else:
        return False


def sync_one_pair(host_path, container_path):
    """Sync a single credential pair. Returns True if file was updated."""
    if not os.path.isfile(host_path):
        return False

    if not os.path.isfile(container_path):
        os.makedirs(os.path.dirname(container_path), exist_ok=True)
        shutil.copy2(host_path, container_path)
        os.chmod(container_path, 0o600)
        log(f"INIT {os.path.basename(container_path)}")
        return True

    if files_identical(host_path, container_path):
        return False

    if is_token_file(container_path):
        if not host_token_is_fresher(host_path, container_path):
            log(f"SKIP {os.path.basename(container_path)} (container token is fresher)")
            return False

    shutil.copy2(host_path, container_path)
    os.chmod(container_path, 0o600)
    log(f"SYNC {os.path.basename(container_path)}")
    return True


def run_sync():
    synced_count = 0
    for host_path, container_path in CREDENTIAL_PAIRS:
        try:
            if sync_one_pair(host_path, container_path):
                synced_count += 1
        except Exception as sync_error:
            log(f"ERROR syncing {os.path.basename(container_path)}: {sync_error}")
    return synced_count


if __name__ == "__main__":
    run_sync()
