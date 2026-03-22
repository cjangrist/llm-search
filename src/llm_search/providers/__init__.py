"""Provider registry mapping provider names to runner functions."""

from llm_search.providers import claude, codex, gemini

PROVIDER_RUNNERS = {
    "claude": claude.run_search,
    "codex": codex.run_search,
    "gemini": gemini.run_search,
}
