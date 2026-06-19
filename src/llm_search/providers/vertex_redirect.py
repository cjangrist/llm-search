"""Vertex AI grounding-redirect resolution, shared across Google-backed providers.

Both the gemini-cli (retired) and the Antigravity CLI (`agy`) emit citation URLs as
Vertex `grounding-api-redirect` links — 250-char redirector URLs that 302 to the real
upstream source. This module follows those redirects (in parallel, with a hard budget)
so end users see the resolved URL, and substitutes them back into the response prose.

Extracted from the former gemini provider so `antigravity.py` can reuse it without
importing a provider module.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import sh

from llm_search.config import VERTEX_REDIRECT_PREFIX

logger = logging.getLogger(__name__)

REDIRECT_PER_URI_TIMEOUT_SECONDS = 5
REDIRECT_OVERALL_TIMEOUT_SECONDS = 30
REDIRECT_PARALLELISM = 4
# Curl exit codes we tolerate silently; anything else gets logged.
# 0=success, 6=DNS resolve, 7=connect, 28=timeout, 35/56/60=TLS/recv issues.
CURL_TOLERATED_EXIT_CODES = {0, 6, 7, 28, 35, 52, 56, 60}

# Vertex grounding-redirect URLs embedded inline in model prose, in markdown-link anchor
# form: `[anchor text](https://vertexaisearch.cloud.google.com/grounding-api-redirect/...)`.
# Token alphabet is base64url + trailing `=` padding. We substitute these in the response
# text so end-users see the resolved upstream URL, not a 250-char Vertex redirect.
VERTEX_REDIRECT_PATTERN = re.compile(
    r"https://vertexaisearch\.cloud\.google\.com/grounding-api-redirect/[A-Za-z0-9_-]+=*"
)


def resolve_redirect(uri):
    """Follow a Vertex grounding redirect to get the actual URL via curl.

    The `-w "%{url_effective}"` write-out flushes the resolved URL to stdout BEFORE curl
    returns its exit code. Google's grounding CDN routinely serves the 302 over HTTP/2
    and then RST_STREAMs the connection (curl exit 92) — that's benign: we already have
    the Location. Read stdout from the sh exception too, so a non-tolerated exit no
    longer drops a perfectly good resolution.
    """
    if not uri or not uri.startswith(VERTEX_REDIRECT_PREFIX):
        return uri
    try:
        result = sh.curl(
            "-sIL", "-o", "/dev/null",
            "-w", "%{url_effective}",
            "--max-time", str(REDIRECT_PER_URI_TIMEOUT_SECONDS),
            uri,
            _ok_code=list(CURL_TOLERATED_EXIT_CODES),
        )
        resolved = str(result).strip()
    except sh.ErrorReturnCode as curl_error:
        stdout_bytes = getattr(curl_error, "stdout", b"") or b""
        resolved = stdout_bytes.decode("utf-8", errors="replace").strip()
        if not resolved or resolved == uri:
            logger.warning("resolve_redirect curl exit %d, no usable url for %s", curl_error.exit_code, uri[:120])
            return uri
        logger.debug("resolve_redirect curl exit %d but recovered url for %s -> %s",
                     curl_error.exit_code, uri[:80], resolved[:80])
    except Exception as resolve_error:
        logger.warning("resolve_redirect failed for %s: %s", uri[:120], resolve_error)
        return uri
    return resolved if resolved and resolved != uri else uri


def resolve_all_uris(uri_list):
    """Resolve redirect URIs in parallel with a hard overall budget.

    Hung redirects no longer stretch the request by minutes — overall budget is bounded
    by REDIRECT_OVERALL_TIMEOUT_SECONDS and parallelism is capped at REDIRECT_PARALLELISM
    so we don't hammer the same redirect host with too many concurrent connections.
    """
    unique_redirect_uris = list({uri for uri in uri_list if uri and uri.startswith(VERTEX_REDIRECT_PREFIX)})
    if not unique_redirect_uris:
        return {}
    logger.debug("Resolving %d unique redirect URIs", len(unique_redirect_uris))
    resolved_map = {}
    with ThreadPoolExecutor(max_workers=REDIRECT_PARALLELISM) as executor:
        pending_futures = {executor.submit(resolve_redirect, uri): uri for uri in unique_redirect_uris}
        try:
            for completed_future in as_completed(pending_futures, timeout=REDIRECT_OVERALL_TIMEOUT_SECONDS):
                original_uri = pending_futures[completed_future]
                resolved_map[original_uri] = completed_future.result()
        except TimeoutError:
            logger.warning(
                "resolve_all_uris hit overall %ds budget — leaving %d/%d URIs unresolved",
                REDIRECT_OVERALL_TIMEOUT_SECONDS,
                len(unique_redirect_uris) - len(resolved_map),
                len(unique_redirect_uris),
            )
            for future, original_uri in pending_futures.items():
                if original_uri not in resolved_map:
                    resolved_map.setdefault(original_uri, original_uri)
                    future.cancel()
    unresolved_count = sum(1 for value in resolved_map.values() if value.startswith(VERTEX_REDIRECT_PREFIX))
    logger.debug("Resolved %d/%d URIs", len(resolved_map) - unresolved_count, len(resolved_map))
    return resolved_map


def replace_inline_redirects_in_text(text, uri_resolution_map):
    """Substitute every Vertex redirect URL in `text` with its resolved form (if known).

    Annotation offsets are NOT updated — accepted drift on substituted spans. The
    annotations point at prose-segment offsets which the model chooses around (not inside)
    markdown-link URLs, so collisions are rare in practice.
    """
    if not uri_resolution_map:
        return text
    return VERTEX_REDIRECT_PATTERN.sub(
        lambda match: uri_resolution_map.get(match.group(0), match.group(0)),
        text,
    )
