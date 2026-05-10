from __future__ import annotations

import json
import sys
from argparse import Namespace
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import HarnessConfig


SESSION_SCHEMA_VERSION = 1


def _now() -> datetime:
    return datetime.now().astimezone()


def create_session_path(*, sessions_dir: Path | str = "sessions", now: datetime | None = None) -> Path:
    base_dir = Path(sessions_dir)
    stamp = (now or _now()).strftime("%Y%m%d_%H%M%S")
    path = base_dir / f"{stamp}.json"
    if not path.exists():
        return path

    suffix = 2
    while True:
        candidate = base_dir / f"{stamp}_{suffix}.json"
        if not candidate.exists():
            return candidate
        suffix += 1


def load_session(path_text: str | Path) -> dict[str, Any]:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"Session file must contain a JSON object: {path}")
    return data


def restore_trace_file(data: dict[str, Any]) -> str | None:
    return _startup_value(data, "trace_file") or _config_value(data, "trace_file")


def restore_mode(data: dict[str, Any]) -> str | None:
    return _startup_value(data, "mode") or _config_value(data, "mode")


def restore_messages(data: dict[str, Any]) -> list[dict[str, Any]]:
    context = data.get("context")
    if not isinstance(context, dict):
        return []
    messages = context.get("messages")
    if not isinstance(messages, list):
        return []
    return [message for message in messages if isinstance(message, dict)]


def restore_loop_count(data: dict[str, Any]) -> int:
    context = data.get("context")
    if not isinstance(context, dict):
        return 0
    try:
        return max(0, int(context.get("loop_count", 0)))
    except (TypeError, ValueError):
        return 0


def save_session(
    path: Path,
    *,
    args: Namespace,
    config: HarnessConfig,
    messages: list[dict[str, Any]],
    loop_count: int,
    resumed_from: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_existing(path)
    created_at = existing.get("created_at") if isinstance(existing.get("created_at"), str) else _now().isoformat()
    data = {
        "schema_version": SESSION_SCHEMA_VERSION,
        "created_at": created_at,
        "updated_at": _now().isoformat(),
        "resumed_from": resumed_from,
        "startup": _startup_snapshot(args),
        "config": _config_snapshot(config),
        "context": {
            "loop_count": loop_count,
            "messages": messages,
        },
    }
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2, default=str)
        file.write("\n")


def _load_existing(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _startup_snapshot(args: Namespace) -> dict[str, Any]:
    return {
        "argv": sys.argv[1:],
        "prompt": list(args.prompt or []),
        "interactive": bool(args.interactive),
        "trace_file": args.trace_file,
        "mode": args.mode,
        "resume_session": args.resume_session,
    }


def _config_snapshot(config: HarnessConfig) -> dict[str, Any]:
    return {
        "env_file": str(config.env_file) if config.env_file is not None else None,
        "provider": config.provider,
        "model_name": config.model_name,
        "model": config.model,
        "api_base": config.api_base,
        "api_key_configured": bool(config.api_key),
        "trace_file": str(config.trace_file),
        "mode": config.mode,
        "artifacts_dir": str(config.artifacts_dir),
        "max_tokens": config.max_tokens,
        "max_iterations": config.max_iterations,
        "model_retries": config.model_retries,
        "system_reinjection_interval": config.system_reinjection_interval,
        "context_compaction_threshold_chars": config.context_compaction_threshold_chars,
        "temperature": config.temperature,
        "reasoning_effort": config.reasoning_effort,
    }


def _startup_value(data: dict[str, Any], key: str) -> str | None:
    startup = data.get("startup")
    if not isinstance(startup, dict):
        return None
    value = startup.get(key)
    return str(value) if value else None


def _config_value(data: dict[str, Any], key: str) -> str | None:
    config = data.get("config")
    if not isinstance(config, dict):
        return None
    value = config.get(key)
    return str(value) if value else None
