from __future__ import annotations

from typing import Any

from .artifact_executor import ArtifactToolExecutor
from .trace_executor import LocalTraceToolExecutor
from .user_executor import UserInteractionToolExecutor


class RoutingToolExecutor:
    def __init__(
        self,
        trace_executor: LocalTraceToolExecutor,
        artifact_executor: ArtifactToolExecutor,
        user_executor: UserInteractionToolExecutor,
    ):
        self.trace_executor = trace_executor
        self.artifact_executor = artifact_executor
        self.user_executor = user_executor

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        if name in {"trace_search", "trace_context"}:
            return self.trace_executor.execute(name, arguments)
        if name == "write_recovered_source":
            return self.artifact_executor.execute(name, arguments)
        if name == "ask_user":
            return self.user_executor.execute(name, arguments)
        return f"Unknown tool: {name}"

    def close(self) -> None:
        self.trace_executor.close()
        self.artifact_executor.close()
        self.user_executor.close()
        return None
