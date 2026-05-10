from __future__ import annotations

from typing import Any, Protocol


class ToolExecutor(Protocol):
    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        ...

    def close(self) -> None:
        ...
