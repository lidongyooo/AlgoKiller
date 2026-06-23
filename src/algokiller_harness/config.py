from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class HarnessConfig:
    env_file: Path | None
    provider: str
    model_name: str
    model: str
    api_key: str
    api_base: str
    trace_file: Path
    trace_dir: Path
    mode: str
    artifacts_dir: Path
    max_tokens: int
    max_iterations: int
    model_retries: int
    system_reinjection_interval: int
    context_compaction_threshold_chars: int
    temperature: float
    reasoning_effort: str


PROVIDER_ALIASES = {
    "openai": "openai",
    "openai-compatible": "openai",
    "openai_compatible": "openai",
    "custom-openai": "openai",
    "custom_openai": "openai",
    "anthropic": "anthropic",
    "google": "google",
    "gemini": "gemini",
}


def _resolve_existing_path(path_text: str, *, label: str) -> Path:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if not path.exists():
        raise ValueError(f"{label} does not exist: {path}")
    return path


def _require_trace_path(*, trace_file: str | None, trace_dir: str | None) -> tuple[Path, Path]:
    if trace_file and trace_dir:
        raise ValueError("Do not combine --trace-file and --trace-dir.")
    path_text = trace_dir or trace_file
    if not path_text:
        raise ValueError("Missing trace path. Start with --trace-dir /path/to/traces or --trace-file /path/to/trace.log.")
    path = _resolve_existing_path(path_text, label="Trace path")
    if trace_dir:
        if not path.is_dir():
            raise ValueError(f"Trace directory path is not a directory: {path}")
        return path, path
    if not path.is_file():
        raise ValueError(f"Trace file path is not a file: {path}")
    return path, path.parent


def _require_mode(mode: str | None) -> str:
    if not mode:
        raise ValueError("Missing analysis mode. Start with --mode ciphertext or general.")
    if mode not in {"ciphertext", "general"}:
        raise ValueError(f"Unsupported analysis mode: {mode}. Use ciphertext or general.")
    return mode


def _model_provider_prefix(provider: str) -> str:
    provider_name = _normalize_provider(provider)
    provider_prefixes = {
        "openai": "openai",
        "anthropic": "anthropic",
        "google": "gemini",
        "gemini": "gemini",
    }
    return provider_prefixes[provider_name]


def _normalize_provider(provider: str) -> str:
    provider_name = provider.strip().lower()
    if provider_name not in PROVIDER_ALIASES:
        supported = ", ".join(sorted(PROVIDER_ALIASES))
        raise ValueError(f"Unsupported model provider: {provider}. Supported providers: {supported}.")
    return PROVIDER_ALIASES[provider_name]


def _model_from_provider_and_name(*, provider: str, model_name: str) -> str:
    name = model_name.strip()
    if not name:
        raise ValueError("Missing model name. Set LITELLM_MODEL_NAME, for example gpt-5.4.")
    if "/" in name:
        return name
    return f"{_model_provider_prefix(provider)}/{name}"


def _provider_from_model(model: str) -> str:
    if "/" not in model:
        return "openai"
    provider, _ = model.split("/", 1)
    return provider


def _load_model_settings() -> tuple[str, str, str]:
    provider = _normalize_provider(os.getenv("LITELLM_PROVIDER", "").strip() or "openai")
    model_name = os.getenv("LITELLM_MODEL_NAME", "").strip()
    if not model_name:
        model_name = "gpt-5.4"
    model = _model_from_provider_and_name(provider=provider, model_name=model_name)
    if "/" in model_name:
        provider = _provider_from_model(model)
        model_name = model.split("/", 1)[1]
    return provider, model_name, model


def _load_api_settings() -> tuple[str, str]:
    return os.getenv("API_KEY", "").strip(), os.getenv("API_BASE", "").strip()


def _load_environment() -> Path | None:
    env_file = os.getenv("HARNESS_ENV_FILE")
    if env_file:
        path = Path(env_file).expanduser().resolve()
        load_dotenv(dotenv_path=path, override=True)
        return path

    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        load_dotenv(dotenv_path=cwd_env, override=True)
        return cwd_env

    return None


def load_config(
    *,
    trace_file: str | None = None,
    trace_dir: str | None = None,
    mode: str | None = None,
) -> HarnessConfig:
    env_file = _load_environment()
    provider, model_name, model = _load_model_settings()
    api_key, api_base = _load_api_settings()
    trace_path, trace_directory = _require_trace_path(trace_file=trace_file, trace_dir=trace_dir)
    return HarnessConfig(
        env_file=env_file,
        provider=provider,
        model_name=model_name,
        model=model,
        api_key=api_key,
        api_base=api_base,
        trace_file=trace_path,
        trace_dir=trace_directory,
        mode=_require_mode(mode),
        artifacts_dir=Path(os.getenv("HARNESS_ARTIFACTS_DIR", "artifacts")),
        max_tokens=int(os.getenv("HARNESS_MAX_TOKENS", "99999")),
        max_iterations=int(os.getenv("HARNESS_MAX_ITERATIONS", "99999")),
        model_retries=max(1, int(os.getenv("HARNESS_MODEL_RETRIES", "5"))),
        system_reinjection_interval=max(1, int(os.getenv("HARNESS_SYSTEM_REINJECTION_INTERVAL", "50"))),
        context_compaction_threshold_chars=max(0, int(os.getenv("HARNESS_CONTEXT_COMPACTION_THRESHOLD_CHARS", "100000"))),
        temperature=float(os.getenv("HARNESS_TEMPERATURE", "0")),
        reasoning_effort=os.getenv("HARNESS_REASONING_EFFORT", "medium"),
    )
