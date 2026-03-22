"""Integration tests for the llm-search Docker service.

Tests /health, /providers, and a live search query against each provider
(claude, codex, gemini) via the OpenAI-compatible /v1/chat/completions endpoint.
"""

import argparse
import logging
import sys
import time

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL = "http://localhost:8041"
TEST_PROMPT = "What is the capital of France? One sentence answer."
REQUEST_TIMEOUT_SECONDS = 300

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"
BOLD = "\033[1m"

logging.basicConfig(
    level=logging.INFO,
    format=f"{CYAN}%(asctime)s{RESET} %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def pass_label(test_name: str) -> None:
    logger.info(f"{GREEN}{BOLD}PASS{RESET}  {test_name}")


def fail_label(test_name: str, reason: str) -> None:
    logger.error(f"{RED}{BOLD}FAIL{RESET}  {test_name}: {reason}")


def test_health(session: requests.Session) -> bool:
    test_name = "GET /health"
    try:
        response = session.get(f"{BASE_URL}/health", timeout=10)
        assert response.status_code == 200, f"HTTP {response.status_code}"
        data = response.json()
        assert data.get("status") == "ok", f"unexpected body: {data}"
        pass_label(test_name)
        return True
    except Exception as error:
        fail_label(test_name, str(error))
        return False


def test_providers(session: requests.Session) -> bool:
    test_name = "GET /providers"
    try:
        response = session.get(f"{BASE_URL}/providers", timeout=10)
        assert response.status_code == 200, f"HTTP {response.status_code}"
        data = response.json()
        expected_providers = {"claude", "codex", "gemini"}
        missing = expected_providers - set(data.keys())
        assert not missing, f"missing providers: {missing}"
        for provider_name, provider_defaults in data.items():
            assert "default_model" in provider_defaults, f"{provider_name} missing default_model"
            assert "default_timeout" in provider_defaults, f"{provider_name} missing default_timeout"
        pass_label(test_name)
        return True
    except Exception as error:
        fail_label(test_name, str(error))
        return False


def test_invalid_model(session: requests.Session) -> bool:
    test_name = "POST /v1/chat/completions — invalid model format returns 400"
    try:
        response = session.post(
            f"{BASE_URL}/v1/chat/completions",
            json={"model": "badformat", "messages": [{"role": "user", "content": "hello"}]},
            timeout=10,
        )
        assert response.status_code == 400, f"expected 400, got {response.status_code}"
        assert "error" in response.json(), "expected error field in response"
        pass_label(test_name)
        return True
    except Exception as error:
        fail_label(test_name, str(error))
        return False


def test_provider_search(session: requests.Session, provider: str, model: str) -> bool:
    test_name = f"POST /v1/chat/completions — {provider}/{model}"
    logger.info(f"{YELLOW}RUNNING{RESET} {test_name} (may take up to {REQUEST_TIMEOUT_SECONDS}s)…")
    started_at = time.monotonic()
    try:
        response = session.post(
            f"{BASE_URL}/v1/chat/completions",
            json={
                "model": f"{provider}/{model}",
                "messages": [{"role": "user", "content": TEST_PROMPT}],
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        elapsed = time.monotonic() - started_at
        assert response.status_code == 200, f"HTTP {response.status_code}: {response.text[:300]}"
        data = response.json()
        assert "choices" in data, f"missing 'choices' in response: {list(data.keys())}"
        assert len(data["choices"]) > 0, "empty choices array"
        content = data["choices"][0]["message"]["content"]
        assert content and len(content) > 5, f"suspiciously short content: {repr(content)}"
        logger.info(f"  answer ({elapsed:.1f}s): {content[:120]}")
        pass_label(test_name)
        return True
    except Exception as error:
        elapsed = time.monotonic() - started_at
        fail_label(test_name, f"{error} ({elapsed:.1f}s elapsed)")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Integration tests for llm-search Docker service")
    parser.add_argument("--base-url", default=BASE_URL, help="Base URL of the service")
    parser.add_argument(
        "--providers",
        nargs="+",
        default=["claude", "codex", "gemini"],
        choices=["claude", "codex", "gemini"],
        help="Which providers to test (default: all)",
    )
    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()
    global BASE_URL
    BASE_URL = arguments.base_url

    logger.info(f"{BOLD}=== llm-search integration tests ==={RESET}")
    logger.info(f"Target: {BASE_URL}")
    logger.info(f"Providers under test: {arguments.providers}")

    session = requests.Session()
    results = []

    results.append(test_health(session))
    results.append(test_providers(session))

    providers_response = session.get(f"{BASE_URL}/providers", timeout=10).json()
    provider_models = {
        provider: providers_response[provider]["default_model"]
        for provider in arguments.providers
        if provider in providers_response
    }
    logger.info(f"Default models from /providers: {provider_models}")
    results.append(test_invalid_model(session))

    for provider in arguments.providers:
        model = provider_models[provider]
        results.append(test_provider_search(session, provider, model))

    passed = sum(results)
    total = len(results)
    color = GREEN if passed == total else RED
    logger.info(f"\n{color}{BOLD}Results: {passed}/{total} passed{RESET}")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
