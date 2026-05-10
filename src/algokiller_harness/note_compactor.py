from __future__ import annotations

import json
from typing import Any

from .agent_prompts import NOTE_COMPACTION_PROMPT
from .model_client import (
    api_kwargs,
    clean_text,
    completion_with_retries,
    extra_body_kwargs,
    message_text,
    reasoning_effort_kwargs,
    temperature_kwargs,
)
from .message_utils import loads_json_object_with_prefix_fallback
from .note_store import NoteStore


class NoteCompactionAgent:
    def __init__(
        self,
        *,
        model: str,
        note_store: NoteStore,
        api_key: str = "",
        api_base: str = "",
        model_retries: int = 5,
        temperature: float = 0,
        reasoning_effort: str = "medium",
    ):
        self.model = model
        self.note_store = note_store
        self.api_key = api_key
        self.api_base = api_base
        self.model_retries = model_retries
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort

    def compact(self, *, messages: list[dict[str, Any]]) -> dict[str, str]:
        arguments = self._request_note_arguments(messages=messages)
        try:
            data = self.note_store.write(arguments)
        except Exception as exc:
            repaired_arguments = self._request_note_arguments(messages=messages, previous_error=str(exc))
            try:
                data = self.note_store.write(repaired_arguments)
            except Exception as repaired_exc:
                raise RuntimeError(f"note store rejected repaired note: {repaired_exc}") from repaired_exc
        return {
            "note_path": str(data.get("note_path") or ""),
            "content": str(data.get("content") or ""),
        }

    def _request_note_arguments(
        self,
        *,
        messages: list[dict[str, Any]],
        previous_error: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"messages": messages}
        if previous_error:
            payload["previous_note_error"] = previous_error
            payload["repair_instruction"] = (
                "Rewrite the JSON so every confirmed item includes a stable trace evidence anchor "
                "(line/address/register/memory/call/hexdump/ret). Artifact paths are not evidence anchors. "
                "Move claims without trace anchors to high_confidence or open_questions."
            )
        response = completion_with_retries(
            max_attempts=self.model_retries,
            model=self.model,
            messages=[
                {"role": "system", "content": NOTE_COMPACTION_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            max_tokens=99999,
            allowed_openai_params=['reasoning_effort'],
            **api_kwargs(api_key=self.api_key, api_base=self.api_base),
            **temperature_kwargs(model=self.model, temperature=self.temperature),
            **reasoning_effort_kwargs(model=self.model, reasoning_effort=self.reasoning_effort),
            **extra_body_kwargs(model=self.model),
        )
        print(response)
        text = clean_text(message_text(response.choices[0].message)).strip()
        print(text)
        try:
            arguments = loads_json_object_with_prefix_fallback(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"note compaction returned invalid JSON") from exc
        if not isinstance(arguments, dict):
            raise RuntimeError("note compaction must return a JSON object")
        return arguments

    def close(self) -> None:
        self.note_store.close()
