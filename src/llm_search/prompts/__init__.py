"""System prompt loading for LLM providers."""

import os

PROMPTS_DIRECTORY = os.path.dirname(os.path.abspath(__file__))


def load_system_prompt():
    """Load the shared system prompt from system_prompt.md."""
    prompt_path = os.path.join(PROMPTS_DIRECTORY, "system_prompt.md")
    with open(prompt_path) as prompt_file:
        return prompt_file.read().strip()
