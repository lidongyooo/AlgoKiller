from __future__ import annotations

from pathlib import Path
from typing import Any


def prompt_user(prompt: str) -> str:
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.history import FileHistory
    except ImportError:
        return input(prompt)

    session = PromptSession(
        history=FileHistory(str(Path.home() / ".algokiller_history")),
        auto_suggest=AutoSuggestFromHistory(),
        enable_history_search=True,
        complete_while_typing=False,
    )
    return session.prompt(prompt)


class UserInteractionToolExecutor:
    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        if name != "ask_user":
            return f"Unknown tool: {name}"

        print("\nClarification needed:")
        print(str(arguments["question"]))
        print("\nWhy this matters:")
        print(str(arguments["why_needed"]))
        needed_items = arguments.get("needed_items") or []
        if needed_items:
            print("\nNeeded items:")
            for item in needed_items:
                print(f"- {item}")
        try:
            answer = prompt_user("\nuser >> ").strip()
        except EOFError:
            return "User did not provide an answer."
        return answer or "User provided an empty answer."

    def close(self) -> None:
        return None
