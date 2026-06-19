"""Provider registry mapping provider names to runner functions."""

from llm_search.providers import antigravity, claude, codex, kimi

PROVIDER_RUNNERS = {
    "claude": claude.run_search,
    "codex": codex.run_search,
    # The standalone gemini CLI was retired for consumer Google accounts; the gemini
    # provider drives the Antigravity CLI (agy) pinned to a Gemini model. The agy
    # integration lives in antigravity.py (it is not exposed as its own provider).
    "gemini": antigravity.run_search_as_gemini,
    "kimi": kimi.run_search,
}
