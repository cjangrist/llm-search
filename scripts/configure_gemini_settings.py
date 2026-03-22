"""Merge required Gemini CLI settings into the synced settings.json.

Called after every credential sync to ensure our overrides survive
the host settings.json being copied in by sync_creds.py.
"""
import json
import os
import sys

SETTINGS_PATH = os.path.join(sys.argv[1], ".gemini", "settings.json") if len(sys.argv) > 1 else os.path.expanduser("~/.gemini/settings.json")

EXCLUDED_TOOLS = [
    "run_shell_command", "glob", "grep_search", "list_directory",
    "read_file", "read_many_files", "replace", "write_file",
    "save_memory", "get_internal_docs", "activate_skill",
    "write_todos", "enter_plan_mode", "exit_plan_mode",
]

CUSTOM_ALIASES = {
    "search-fast": {
        "extends": "chat-base-3",
        "modelConfig": {
            "model": "gemini-3-flash-preview",
            "generateContentConfig": {
                "thinkingConfig": {
                    "thinkingLevel": "MINIMAL",
                }
            }
        }
    }
}


def apply_settings(settings_path):
    settings = {}
    if os.path.isfile(settings_path):
        with open(settings_path) as f:
            settings = json.load(f)

    settings.setdefault("admin", {})
    settings["admin"]["extensions"] = {"enabled": False}
    settings["admin"]["mcp"] = {"enabled": False}
    settings["mcpServers"] = {}

    settings.setdefault("tools", {})
    settings["tools"]["exclude"] = EXCLUDED_TOOLS
    settings["tools"]["shell"] = {"enableInteractiveShell": False}

    settings.setdefault("modelConfigs", {})
    settings["modelConfigs"]["customAliases"] = CUSTOM_ALIASES

    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)


if __name__ == "__main__":
    apply_settings(SETTINGS_PATH)
