from __future__ import annotations

import json
from typing import Any

from .agent_prompts import ASK_USER_REVIEW_PROMPT
from .message_utils import dict_message_text, loads_json_object_with_prefix_fallback
from .model_client import (
    api_kwargs,
    clean_text,
    completion_with_retries,
    extra_body_kwargs,
    message_text,
    reasoning_effort_kwargs,
    temperature_kwargs,
)


class AskUserReviewAgent:
    def __init__(
        self,
        *,
        model: str,
        mode: str,
        api_key: str = "",
        api_base: str = "",
        max_tokens: int = 99999,
        model_retries: int = 5,
        temperature: float = 0,
        reasoning_effort: str = "medium",
    ):
        self.model = model
        self.mode = mode
        self.api_key = api_key
        self.api_base = api_base
        self.max_tokens = max_tokens
        self.model_retries = model_retries
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort

    def review(self, *, messages: list[dict[str, Any]], arguments: dict[str, Any]) -> dict[str, str]:
        payload = {
            "analysis_mode": self.mode,
            "initial_user_prompt": self._initial_user_prompt(messages),
            "ask_user_arguments": arguments,
        }
        response = completion_with_retries(
            max_attempts=self.model_retries,
            model=self.model,
            messages=[
                {"role": "system", "content": ASK_USER_REVIEW_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            max_tokens=4096,
            allowed_openai_params=['reasoning_effort'],
            **api_kwargs(api_key=self.api_key, api_base=self.api_base),
            **temperature_kwargs(model=self.model, temperature=self.temperature),
            **reasoning_effort_kwargs(model=self.model, reasoning_effort=self.reasoning_effort),
            **extra_body_kwargs(model=self.model),
        )
        text = clean_text(message_text(response.choices[0].message)).strip()
        try:
            data = loads_json_object_with_prefix_fallback(text)
        except json.JSONDecodeError:
            print("> ask_user_review_parse_error_raw")
            print(text)
            return {
                "decision": "ask_user",
                "reason": "验收 agent 未能给出可解析判断，已打印返回原文。",
                "instruction": "",
            }
        decision = str(data.get("decision") or "ask_user").strip()
        if decision not in {"continue", "ask_user"}:
            decision = "ask_user"
        return {
            "decision": decision,
            "reason": str(data.get("reason") or ""),
            "instruction": str(data.get("instruction") or ""),
        }

    def _initial_user_prompt(self, messages: list[dict[str, Any]]) -> str:
        for message in messages:
            if message.get("role") == "user":
                return dict_message_text(message)
        return ""
