from __future__ import annotations

import json
import time
from itertools import cycle
from threading import Lock
from typing import Any, Iterator

import litellm

_api_key_lock = Lock()
_api_key_rotators: dict[tuple[str, ...], Iterator[str]] = {}


def message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(str(item.get("text", "")))
        return "".join(text_parts)
    return ""


def clean_text(value: str) -> str:
    """Remove Unicode surrogate code points before sending text to LiteLLM."""
    return value.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def clean_jsonable(value: Any) -> Any:
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return [clean_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {clean_jsonable(key): clean_jsonable(item) for key, item in value.items()}
    return value


def api_kwargs(*, api_key: str, api_base: str) -> dict[str, str]:
    kwargs = {}
    rotated_api_key = _next_api_key(api_key)
    if rotated_api_key:
        kwargs["api_key"] = rotated_api_key
    if api_base:
        kwargs["api_base"] = api_base
    return kwargs


def _api_keys(api_key: str) -> tuple[str, ...]:
    return tuple(key.strip() for key in api_key.split(",") if key.strip())


def _next_api_key(api_key: str) -> str:
    keys = _api_keys(api_key)
    if not keys:
        return ""
    if len(keys) == 1:
        return keys[0]
    with _api_key_lock:
        rotator = _api_key_rotators.setdefault(keys, cycle(keys))
        return next(rotator)


def temperature_kwargs(*, model: str, temperature: float) -> dict[str, float]:
    if (_is_gpt5_family_model(model) or _is_kimi_model(model)) and temperature != 1:
        return {}
    return {"temperature": temperature}


def reasoning_effort_kwargs(*, model: str, reasoning_effort: str) -> dict[str, str]:
    if not reasoning_effort or reasoning_effort.lower() in {"none", "off", "disabled"}:
        return {}
    if _is_gpt5_family_model(model):
        return {"reasoning_effort": reasoning_effort, "allowed_openai_params": ["reasoning_effort"]}
    elif _is_anthropic_model(model):
        return {"reasoning_effort": reasoning_effort}

    return {}


def extra_body_kwargs(*, model: str) -> dict[str, dict[str, Any]]:
    if _is_kimi_model(model) or is_deepseek(model):
        return {"extra_body": {"thinking": {"type": "enabled"}}}
    return {}


def completion_with_retries(*, max_attempts: int, retry_delay_seconds: float = 1.0, **kwargs: Any) -> Any:
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        try:
            return litellm.completion(**kwargs)
        except Exception as exc:
            if _is_non_retryable_model_error(exc) or attempt >= attempts:
                raise
            print(
                "> model_request_retry("
                + json.dumps(
                    {
                        "model": kwargs.get("model"),
                        "attempt": attempt + 1,
                        "max_attempts": attempts,
                    },
                    ensure_ascii=False,
                )
                + ")"
            )
            if retry_delay_seconds > 0:
                time.sleep(retry_delay_seconds)

    raise RuntimeError("unreachable model retry state")


def _is_gpt5_family_model(model: str) -> bool:
    return model.split("/", 1)[0].lower() == "openai"


def is_deepseek(model: str) -> bool:
    model_name = model.rsplit("/", 1)[-1].lower()
    return model_name.startswith("deepseek")


def _is_anthropic_model(model: str) -> bool:
    return model.split("/", 1)[0].lower() == "anthropic"


def _is_kimi_model(model: str) -> bool:
    model_name = model.rsplit("/", 1)[-1].lower()
    return model_name.startswith("kimi-")


def _is_non_retryable_model_error(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = (
        "api_key",
        "authentication",
        "model not found",
        "模型选择错误",
        "all available accounts exhausted",
    )
    res = any(marker in text for marker in markers)
    print(res)
    return res
