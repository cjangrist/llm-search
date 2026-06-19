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
import tempfile
import time


CONTAINER_HOME = os.getenv("SYNC_TARGET_HOME", "/home/llmsearch")

CREDENTIAL_PAIRS = [
    ("/mnt/creds/claude/.credentials.json", f"{CONTAINER_HOME}/.claude/.credentials.json"),
    ("/mnt/creds/codex/auth.json", f"{CONTAINER_HOME}/.codex/auth.json"),
    ("/mnt/creds/codex/config.toml", f"{CONTAINER_HOME}/.codex/config.toml"),
    ("/mnt/creds/gemini/oauth_creds.json", f"{CONTAINER_HOME}/.gemini/oauth_creds.json"),
    ("/mnt/creds/gemini/google_accounts.json", f"{CONTAINER_HOME}/.gemini/google_accounts.json"),
    ("/mnt/creds/kimi/config.toml", f"{CONTAINER_HOME}/.kimi/config.toml"),
    ("/mnt/creds/kimi/credentials/kimi-code.json", f"{CONTAINER_HOME}/.kimi/credentials/kimi-code.json"),
    # Antigravity (agy CLI) OAuth token — lives under the same ~/.gemini mount.
    ("/mnt/creds/gemini/antigravity-cli/antigravity-oauth-token", f"{CONTAINER_HOME}/.gemini/antigravity-cli/antigravity-oauth-token"),
    # Grok (xAI CLI) — OAuth session token + config + model cache under ~/.grok.
    ("/mnt/creds/grok/auth.json", f"{CONTAINER_HOME}/.grok/auth.json"),
    ("/mnt/creds/grok/config.toml", f"{CONTAINER_HOME}/.grok/config.toml"),
    ("/mnt/creds/grok/models_cache.json", f"{CONTAINER_HOME}/.grok/models_cache.json"),
]

TOKEN_FILES = {
    ".credentials.json",
    "auth.json",
    "oauth_creds.json",
    "kimi-code.json",
    "antigravity-oauth-token",
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


def extract_kimi_expiry(filepath):
    """Extract access_token JWT exp (converted to ms) from Kimi credentials/kimi-code.json."""
    try:
        with open(filepath) as credential_file:
            data = json.load(credential_file)
        access_token = data.get("access_token", "")
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


def extract_antigravity_expiry(filepath):
    """Extract token.expiry (ISO8601 -> ms epoch) from antigravity-oauth-token."""
    try:
        with open(filepath) as credential_file:
            data = json.load(credential_file)
        expiry = data.get("token", {}).get("expiry", "")
        if not expiry:
            return 0
        # Normalize Go's RFC3339-with-nanoseconds (e.g. 2026-06-19T12:44:18.067257208Z)
        # to something datetime.fromisoformat accepts: Z -> +00:00, fractional secs to 6 digits.
        import re
        from datetime import datetime
        normalized = re.sub(r"(\.\d{6})\d+", r"\1", expiry.replace("Z", "+00:00"))
        return int(datetime.fromisoformat(normalized).timestamp() * 1000)
    except (json.JSONDecodeError, OSError, KeyError, ValueError):
        return 0


def extract_grok_expiry(filepath):
    """Extract the latest token expiry (ISO8601 expires_at -> ms) from Grok auth.json.

    Grok keys each credential by issuer URL ("https://auth.x.ai::<uuid>"); each entry carries
    an `expires_at` RFC3339 timestamp. Return the max across entries (ms epoch). auth.json's
    basename collides with codex, so select_expiry_extractor routes by path to reach this.
    """
    try:
        with open(filepath) as credential_file:
            data = json.load(credential_file)
        import re
        from datetime import datetime
        expiries = []
        for entry in data.values():
            if not isinstance(entry, dict):
                continue
            raw_expiry = entry.get("expires_at", "")
            if not raw_expiry:
                continue
            normalized = re.sub(r"(\.\d{6})\d+", r"\1", raw_expiry.replace("Z", "+00:00"))
            expiries.append(int(datetime.fromisoformat(normalized).timestamp() * 1000))
        return max(expiries) if expiries else 0
    except (json.JSONDecodeError, OSError, KeyError, ValueError):
        return 0


EXPIRY_EXTRACTORS = {
    ".credentials.json": extract_claude_expiry,
    "auth.json": extract_codex_expiry,
    "oauth_creds.json": extract_gemini_expiry,
    "kimi-code.json": extract_kimi_expiry,
    "antigravity-oauth-token": extract_antigravity_expiry,
}


def select_expiry_extractor(container_path):
    """Pick the expiry extractor for a container cred path.

    `auth.json` is ambiguous — both codex (~/.codex) and grok (~/.grok) use that basename with
    different JSON shapes — so disambiguate by directory before the basename fallback.
    """
    if container_path.endswith("/.grok/auth.json"):
        return extract_grok_expiry
    return EXPIRY_EXTRACTORS.get(os.path.basename(container_path))


def is_token_file(filepath):
    return os.path.basename(filepath) in TOKEN_FILES


def host_token_is_fresher(host_path, container_path):
    """Return True if the host token has a later expiry than the container token."""
    extractor = select_expiry_extractor(container_path)
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


def atomic_copy(host_path, container_path, mode=0o600):
    """Copy host_path -> container_path via tempfile + os.replace (POSIX-atomic same-FS).

    Avoids the read-mid-write window of shutil.copy2 (which truncates the destination,
    then writes byte-by-byte). Mode is fchmod'd before the rename so there is no umask
    window where the file is briefly world/group readable.
    """
    container_dir = os.path.dirname(container_path)
    os.makedirs(container_dir, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=".sync_", suffix=os.path.basename(container_path), dir=container_dir)
    try:
        os.fchmod(tmp_fd, mode)
        with open(host_path, "rb") as src, os.fdopen(tmp_fd, "wb") as dst:
            tmp_fd = -1  # ownership transferred to dst's contextmanager
            shutil.copyfileobj(src, dst)
        try:
            shutil.copystat(host_path, container_path) if os.path.exists(container_path) else None
        except OSError:
            pass
        os.replace(tmp_path, container_path)
        os.chmod(container_path, mode)
    except Exception:
        if tmp_fd >= 0:
            try: os.close(tmp_fd)
            except OSError: pass
        if os.path.exists(tmp_path):
            try: os.replace(tmp_path, tmp_path + ".aborted")
            except OSError: pass
        raise


def sync_one_pair(host_path, container_path):
    """Sync a single credential pair. Returns True if file was updated."""
    if not os.path.isfile(host_path):
        return False

    if not os.path.isfile(container_path):
        atomic_copy(host_path, container_path)
        log(f"INIT {os.path.basename(container_path)}")
        return True

    if files_identical(host_path, container_path):
        return False

    if is_token_file(container_path):
        if not host_token_is_fresher(host_path, container_path):
            log(f"SKIP {os.path.basename(container_path)} (container token is fresher)")
            return False

    atomic_copy(host_path, container_path)
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
