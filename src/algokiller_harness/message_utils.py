from __future__ import annotations

import json
from typing import Any


def assistant_message(message: Any) -> dict[str, Any]:
    data = {
        "role": "assistant",
        "content": getattr(message, "content", None) or "",
    }
    reasoning_content = getattr(message, "reasoning_content", None)
    if reasoning_content:
        data["reasoning_content"] = reasoning_content
    provider_specific_fields = getattr(message, "provider_specific_fields", None)
    if provider_specific_fields:
        data["provider_specific_fields"] = (
            provider_specific_fields.model_dump()
            if hasattr(provider_specific_fields, "model_dump")
            else provider_specific_fields
        )
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        data["tool_calls"] = [call.model_dump() if hasattr(call, "model_dump") else call for call in tool_calls]
    return data


def tool_call_name_and_args(tool_call: Any) -> tuple[str, dict[str, Any]]:
    function = getattr(tool_call, "function", None)
    if function is None and isinstance(tool_call, dict):
        function = tool_call.get("function", {})

    name = getattr(function, "name", None) if not isinstance(function, dict) else function.get("name")
    raw_args = getattr(function, "arguments", None) if not isinstance(function, dict) else function.get("arguments")
    if not name:
        raise ValueError(f"Tool call missing function name: {tool_call!r}")
    if isinstance(raw_args, str):
        arguments = json.loads(raw_args or "{}")
    elif isinstance(raw_args, dict):
        arguments = raw_args
    else:
        arguments = {}
    return name, arguments


def tool_call_id(tool_call: Any) -> str:
    if isinstance(tool_call, dict):
        return str(tool_call["id"])
    return str(tool_call.id)


def tool_call_id_or_placeholder(tool_call: Any) -> str:
    try:
        return tool_call_id(tool_call)
    except Exception:
        return "invalid_tool_call"


def known_tool_names(tools: list[dict[str, Any]]) -> set[str]:
    names = set()
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict) and function.get("name"):
            names.add(str(function["name"]))
    return names


def dict_message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(str(item.get("text", "")))
        return "".join(text_parts)
    return ""


def loads_json_object_with_prefix_fallback(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find('{"')
        if start < 0:
            raise
        return json.loads(text[start:])


def system_reinjection_message(system_prompt: str) -> str:
    return (
        "系统提示刷新：不要重启任务，不要丢弃已有证据。继续当前 trace、当前 mode、当前用户目标。\n"
        "下面是完整系统约束，请重新遵守：\n"
        f"{system_prompt}"
    )


def format_tool_output_for_console(output: str) -> str:
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return output[:500]

    if isinstance(data, dict) and isinstance(data.get("stdout"), str):
        stdout_lines = _json_lines_or_none(data["stdout"])
        if stdout_lines is not None:
            data = {**data, "stdout": stdout_lines}

    formatted = _compact_json(data)
    if len(formatted) <= 200 or not isinstance(data, dict) or "stdout" not in data:
        return formatted
    return _truncate_stdout_for_console(data, max_chars=200)


def _json_lines_or_none(value: str) -> list[Any] | None:
    lines = [line for line in value.splitlines() if line.strip()]
    if not lines:
        return None
    parsed = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            return None
    return parsed


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _truncate_stdout_for_console(data: dict[str, Any], *, max_chars: int) -> str:
    marker = "...[truncated]"
    stdout = data.get("stdout", "")
    if isinstance(stdout, str):
        stdout_text = stdout
    else:
        stdout_text = _compact_json(stdout)

    marker_result = _compact_json({**data, "stdout": marker})
    if len(marker_result) > max_chars:
        return marker_result

    low = 0
    high = len(stdout_text)
    best = marker
    while low <= high:
        mid = (low + high) // 2
        candidate = stdout_text[:mid] + marker
        formatted = _compact_json({**data, "stdout": candidate})
        if len(formatted) <= max_chars:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return _compact_json({**data, "stdout": best})
