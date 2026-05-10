from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any


class NoteStore:
    NOTE_NAME_PATTERN = re.compile(r"^\d{8}_\d{6}_note(?:_\d+)?\.md$")
    ANCHOR_PATTERN = re.compile(
        r"("
        r"\bline\s+\d+"
        r"|\bline:\s*\d+"
        r"|0x[0-9a-fA-F]+"
        r"|\bmem_[rw]\b"
        r"|\bx(?:[0-9]|[12][0-9]|30)\b"
        r"|\bcall"
        r"|\bhexdump\b"
        r"|\bret"
        r")"
    )

    def __init__(self, notes_dir: Path | str = "notes"):
        self.notes_dir = Path(notes_dir).resolve()

    def write(self, arguments: dict[str, Any]) -> dict[str, Any]:
        task = self._required_text(arguments, "task")
        confirmed = self._required_items(arguments, "confirmed")
        next_steps = self._required_items(arguments, "next_steps")
        high_confidence = self._optional_items(arguments, "high_confidence")
        excluded = self._optional_items(arguments, "excluded")
        open_questions = self._optional_items(arguments, "open_questions")

        # missing_anchor_items = [item for item in confirmed if not self.ANCHOR_PATTERN.search(item)]
        # if missing_anchor_items:
        #     raise ValueError(
        #         "confirmed items must include stable evidence anchors "
        #         f"(line/address/register/memory/call/hexdump/ret): {missing_anchor_items}"
        #     )

        self.notes_dir.mkdir(parents=True, exist_ok=True)
        target = self._allocate_note_path()
        content = self._format_note(
            task=task,
            confirmed=confirmed,
            high_confidence=high_confidence,
            excluded=excluded,
            open_questions=open_questions,
            next_steps=next_steps,
        )
        target.write_text(content, encoding="utf-8")
        return {
            "note_path": str(target),
            "content": content,
            "confirmed_count": len(confirmed),
            "next_steps_count": len(next_steps),
        }

    def close(self) -> None:
        return None

    def _stamp(self) -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    def _allocate_note_path(self) -> Path:
        stamp = self._stamp()
        for index in range(1000):
            suffix = f"_{index}" if index else ""
            candidate = (self.notes_dir / f"{stamp}_note{suffix}.md").resolve()
            if not candidate.is_relative_to(self.notes_dir):
                raise RuntimeError(f"note path escapes notes directory: {candidate}")
            if not candidate.exists():
                return candidate
        raise RuntimeError("could not allocate a timestamped note path")

    def _format_note(
        self,
        *,
        task: str,
        confirmed: list[str],
        high_confidence: list[str],
        excluded: list[str],
        open_questions: list[str],
        next_steps: list[str],
    ) -> str:
        lines = [
            "# AlgoKiller Progress Note",
            "",
            f"- Created: {datetime.now().astimezone().isoformat()}",
            f"- Task: {task}",
            "",
        ]
        sections = [
            ("## Confirmed", confirmed),
            ("## High Confidence", high_confidence),
            ("## Excluded", excluded),
            ("## Open Questions", open_questions),
            ("## Next Steps", next_steps),
        ]
        for title, items in sections:
            lines.append(title)
            if items:
                lines.extend(f"- {item}" for item in items)
            else:
                lines.append("- None")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _required_text(self, arguments: dict[str, Any], name: str) -> str:
        if name not in arguments:
            raise ValueError(f"note missing required field: {name}")
        text = str(arguments[name]).strip()
        if not text:
            raise ValueError(f"note {name} must not be empty")
        return text

    def _required_items(self, arguments: dict[str, Any], name: str) -> list[str]:
        if name not in arguments:
            raise ValueError(f"note missing required field: {name}")
        items = self._items(arguments.get(name), name)
        if not items:
            raise ValueError(f"note {name} must contain at least one item")
        return items

    def _optional_items(self, arguments: dict[str, Any], name: str) -> list[str]:
        return self._items(arguments.get(name, []), name)

    def _items(self, value: Any, name: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError(f"note {name} must be an array of strings")
        items = [str(item).strip() for item in value if str(item).strip()]
        if len(items) != len(value):
            raise ValueError(f"note {name} must not contain empty items")
        return items
