from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


class ArtifactToolExecutor:
    def __init__(self, artifacts_dir: Path, mode: str = "unknown"):
        self.artifacts_dir = artifacts_dir.resolve()
        self.mode = self._safe_mode(mode)

    def _safe_mode(self, mode: str) -> str:
        text = "".join(char if char.isalnum() else "_" for char in str(mode).strip().upper())
        return text or "UNKNOWN"

    def _stamp(self) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{self.mode}_{timestamp}"

    def _timestamped_relative_path(self, rel_path: Path, stamp: str, index: int = 0) -> Path:
        suffix = f"_{index}" if index else ""
        return rel_path.with_name(f"{rel_path.stem}_{stamp}{suffix}{rel_path.suffix}")

    def _final_markdown_path(self, stamp: str, index: int = 0) -> Path:
        suffix = f"_{index}" if index else ""
        return self.artifacts_dir / f"{stamp}{suffix}.md"

    def write_final_markdown(self, content: str) -> Path:
        target = None
        stamp = self._stamp()
        for index in range(1000):
            candidate = self._final_markdown_path(stamp, index).resolve()
            if not candidate.is_relative_to(self.artifacts_dir):
                raise RuntimeError(f"final markdown path escapes artifacts directory: {candidate}")
            if not candidate.exists():
                target = candidate
                break
        if target is None:
            raise RuntimeError("could not allocate a timestamped final markdown artifact path")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        if name != "write_recovered_source":
            return f"Unknown tool: {name}"
        if "path" not in arguments:
            return json.dumps(
                {
                    "status": "error",
                    "error": "write_recovered_source missing required argument: path",
                    "instruction": "Retry write_recovered_source with both path and source when final code is ready.",
                },
                ensure_ascii=False,
            )
        if "source" not in arguments:
            return json.dumps(
                {
                    "status": "error",
                    "error": "write_recovered_source missing required argument: source",
                    "instruction": "Retry write_recovered_source with both path and source when final code is ready.",
                },
                ensure_ascii=False,
            )
        rel_path = Path(str(arguments["path"]))
        if rel_path.is_absolute():
            return "Error: path must be relative to the artifacts directory"
        if rel_path.suffix != ".py":
            return "Error: recovered source path must end with .py"

        target = None
        stamp = self._stamp()
        for index in range(1000):
            timestamped_rel_path = self._timestamped_relative_path(rel_path, stamp, index)
            candidate = (self.artifacts_dir / timestamped_rel_path).resolve()
            if not candidate.is_relative_to(self.artifacts_dir):
                return f"Error: path escapes artifacts directory: {rel_path}"
            if not candidate.exists():
                target = candidate
                break
        if target is None:
            return f"Error: could not allocate a timestamped artifact path for: {rel_path}"

        target.parent.mkdir(parents=True, exist_ok=True)
        source = str(arguments["source"])
        target.write_text(source, encoding="utf-8")

        notes = arguments.get("notes")
        note_path = None
        if notes:
            note_path = target.with_suffix(".notes.md")
            note_path.write_text(str(notes), encoding="utf-8")

        return json.dumps(
            {"status": "ok", "source_path": str(target), "notes_path": str(note_path) if note_path else None},
            ensure_ascii=False,
        )

    def close(self) -> None:
        return None
