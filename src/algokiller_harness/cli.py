from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from .artifact_executor import ArtifactToolExecutor
from .ask_user_reviewer import AskUserReviewAgent
from .config import HarnessConfig, load_config
from .note_compactor import NoteCompactionAgent
from .note_store import NoteStore
from .prompts import SUPPORTED_ANALYSIS_MODES, build_system_prompt
from .routing_executor import RoutingToolExecutor
from .session_store import (
    create_session_path,
    load_session,
    restore_loop_count,
    restore_messages,
    restore_mode,
    restore_trace_dir,
    restore_trace_file,
    save_session,
)
from .tool_schemas import (
    ASK_USER_TOOL,
    RECOVERED_SOURCE_TOOL,
    TRACE_ALL_SEARCH_TOOL,
    TRACE_CONTEXT_TOOL,
    TRACE_FILES_TOOL,
    TRACE_SEARCH_TOOL,
)
from .trace_agent import TraceAgent
from .trace_executor import LocalTraceToolExecutor
from .user_executor import UserInteractionToolExecutor


def _credential_help_for_model(model: str) -> str:
    return (
        f"Model={model}. Set the generic API_KEY in .env or in the shell, for example:\n"
        "  export API_KEY=...\n"
        "Set API_BASE too when your endpoint is not the provider default."
    )


def _provider_from_model(model: str | None) -> str:
    if not model or "/" not in model:
        return "openai"
    return model.split("/", 1)[0].lower()


def _model_name_from_model(model: str | None) -> str:
    if not model:
        return "the-gateway-model-name"
    return model.rsplit("/", 1)[-1]


def _route_notes(*, provider: str, model: str, api_base: str) -> list[str]:
    if not api_base.strip():
        return []
    provider_name = provider.strip().lower()
    notes = [
        f"Custom API_BASE is set: {api_base}. LiteLLM will still use the {provider_name} protocol selected by LITELLM_PROVIDER."
    ]
    if provider_name in {"anthropic", "gemini", "google"}:
        notes.append(
            "If this endpoint is an OpenAI-compatible gateway, set LITELLM_PROVIDER=openai "
            f"and LITELLM_MODEL_NAME={_model_name_from_model(model)} instead. "
            "Use an API_BASE ending in /v1 when the gateway expects OpenAI's /v1/chat/completions path."
        )
    return notes


def _route_help_for_failure(*, model: str | None, api_base: str = "") -> str:
    provider = _provider_from_model(model)
    if not api_base.strip():
        return ""
    if provider in {"anthropic", "gemini"}:
        return (
            "\nA custom API_BASE does not change the protocol LiteLLM uses. "
            f"Configured model={model} sends {provider} protocol requests to API_BASE. "
            "For an OpenAI-compatible gateway, use LITELLM_PROVIDER=openai and set "
            f"LITELLM_MODEL_NAME={_model_name_from_model(model)}."
        )
    if provider == "openai" and not api_base.rstrip("/").endswith("/v1"):
        return "\nIf the endpoint returns 404, try API_BASE with the provider's /v1 base path."
    return ""


def _validate_model_credentials(model: str, *, api_key: str) -> None:
    if api_key.strip():
        return
    raise RuntimeError(_credential_help_for_model(model))


def _format_agent_error(exc: Exception, *, model: str | None = None, api_base: str = "") -> str:
    text = str(exc)
    lowered = text.lower()
    route_help = _route_help_for_failure(model=model, api_base=api_base)
    if (
        "model not found" in lowered
        or "notfounderror" in lowered
        or "404" in lowered
        or "模型选择错误" in text
    ):
        return (
            f"Model selection failed for {model or 'the configured model'}.\n"
            "The API endpoint accepted the request path but does not recognize this model name. "
            "Set LITELLM_MODEL_NAME to a model supported by API_BASE."
            f"{route_help}"
        )
    if "all available accounts exhausted" in lowered:
        return (
            "Model provider account quota is exhausted.\n"
            "The model route is valid, but the API key/base endpoint has no available upstream account quota."
        )
    if "api_key" in lowered or "authentication" in lowered:
        help_text = _credential_help_for_model(model) if model else (
            "Configure your LiteLLM provider credentials first."
        )
        return (
            "Model authentication failed.\n"
            f"{help_text}\n"
            "Then restart algokiller."
            f"{route_help}"
        )
    return f"Model request failed: {exc}{route_help}"


def _print_route_notes(config: HarnessConfig) -> None:
    for note in _route_notes(provider=config.provider, model=config.model, api_base=config.api_base):
        print(f"Config note: {note}", file=sys.stderr)


def _build_agent_from_config(config: HarnessConfig) -> TraceAgent:
    _validate_model_credentials(config.model, api_key=config.api_key)
    env_file_text = str(config.env_file) if config.env_file is not None else "not found"
    print(
        f"AlgoKiller config. Env={env_file_text}. Model={config.model}. "
        f"API_BASE={config.api_base or 'provider default'}. MaxTokens={config.max_tokens}. "
        f"Retries={config.model_retries}. CompactionThresholdChars={config.context_compaction_threshold_chars}."
    )
    _print_route_notes(config)
    artifact_executor = ArtifactToolExecutor(config.artifacts_dir, mode=config.mode)
    executor = RoutingToolExecutor(
        trace_executor=LocalTraceToolExecutor(config.artifacts_dir, config.trace_file),
        artifact_executor=artifact_executor,
        user_executor=UserInteractionToolExecutor(),
    )
    agent = TraceAgent(
        model=config.model,
        tools=[
            TRACE_FILES_TOOL,
            TRACE_ALL_SEARCH_TOOL,
            TRACE_SEARCH_TOOL,
            TRACE_CONTEXT_TOOL,
            ASK_USER_TOOL,
            RECOVERED_SOURCE_TOOL,
        ],
        executor=executor,
        ask_user_reviewer=AskUserReviewAgent(
            model=config.model,
            mode=config.mode,
            api_key=config.api_key,
            api_base=config.api_base,
            model_retries=config.model_retries,
            temperature=config.temperature,
            reasoning_effort=config.reasoning_effort,
        ),
        max_tokens=config.max_tokens,
        max_iterations=config.max_iterations,
        model_retries=config.model_retries,
        system_reinjection_interval=config.system_reinjection_interval,
        temperature=config.temperature,
        reasoning_effort=config.reasoning_effort,
        api_key=config.api_key,
        api_base=config.api_base,
        system_prompt=build_system_prompt(config.mode),
        final_text_callback=artifact_executor.write_final_markdown,
        note_compactor=NoteCompactionAgent(
            model=config.model,
            note_store=NoteStore(),
            api_key=config.api_key,
            api_base=config.api_base,
            model_retries=config.model_retries,
            temperature=config.temperature,
            reasoning_effort=config.reasoning_effort,
        ),
        context_compaction_threshold_chars=config.context_compaction_threshold_chars,
    )
    bound_context_message = {
        "role": "system",
        "content": (
            f"Session trace directory is already bound by the harness: {config.trace_dir}. "
            f"Startup trace path: {config.trace_file}. "
            f"Analysis mode is fixed at startup: {config.mode}. "
            "Do not ask tool calls to provide a trace path or change analysis mode."
        ),
    }
    agent.messages.append(bound_context_message)
    agent.startup_context_messages = [
        {"role": "system", "content": build_system_prompt(config.mode)},
        bound_context_message,
    ]
    return agent


def build_agent(trace_file: str, mode: str) -> TraceAgent:
    return _build_agent_from_config(load_config(trace_file=trace_file, mode=mode))


def _ask_agent(agent: TraceAgent, prompt: str, *, output: Callable[[str], None] = print) -> bool:
    try:
        output(agent.ask(prompt))
        return True
    except Exception as exc:
        output(_format_agent_error(exc, model=agent.model, api_base=agent.api_base))
        return False


def _continue_agent(agent: TraceAgent, *, output: Callable[[str], None] = print) -> bool:
    try:
        output(agent.continue_conversation())
        return True
    except Exception as exc:
        output(_format_agent_error(exc, model=agent.model, api_base=agent.api_base))
        return False


def _build_prompt_session():
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.history import FileHistory
    except ImportError as exc:
        raise RuntimeError(
            "Interactive input requires prompt-toolkit. Install runtime dependencies with: "
            "python -m pip install litellm prompt-toolkit python-dotenv"
        ) from exc

    history_path = Path.home() / ".algokiller_history"
    return PromptSession(
        history=FileHistory(str(history_path)),
        auto_suggest=AutoSuggestFromHistory(),
        enable_history_search=True,
        complete_while_typing=False,
    )


def _bind_session_writer(
    *,
    agent: TraceAgent,
    session_path: Path,
    args: argparse.Namespace,
    config: HarnessConfig,
    resumed_from: str | None,
) -> Callable[[], None]:
    def write_session() -> None:
        save_session(
            session_path,
            args=args,
            config=config,
            messages=agent.messages,
            loop_count=agent.loop_count,
            resumed_from=resumed_from,
        )

    def on_context_updated(updated_agent: TraceAgent) -> None:
        save_session(
            session_path,
            args=args,
            config=config,
            messages=updated_agent.messages,
            loop_count=updated_agent.loop_count,
            resumed_from=resumed_from,
        )

    agent.context_update_callback = on_context_updated
    return write_session


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Single-agent arm64 trace directory analysis harness.")
    parser.add_argument("prompt", nargs="*", help="One-shot task prompt.")
    parser.add_argument("-i", "--interactive", action="store_true", help="Start an interactive session.")
    parser.add_argument(
        "--trace-file",
        help=(
            "Trace log file for this conversation. Deprecated for multi-file traces: "
            "the harness opens this file's parent directory and indexes every .log file there."
        ),
    )
    parser.add_argument(
        "--trace-dir",
        help="Trace directory for this conversation. The harness indexes every .log file in this directory.",
    )
    parser.add_argument(
        "--mode",
        choices=SUPPORTED_ANALYSIS_MODES,
        help=(
            "Analysis mode: ciphertext recovers encryption/plaintext; "
            "general handles open-ended trace analysis."
        ),
    )
    parser.add_argument(
        "--resume-session",
        help="Resume a conversation from a saved sessions/*.json file.",
    )
    return parser


def _effective_trace_scope(path_text: str) -> Path:
    path = Path(path_text).expanduser().resolve()
    return path if path.is_dir() else path.parent


def _apply_resume_session_args(args: argparse.Namespace, session_data: dict[str, object]) -> None:
    trace_file = restore_trace_file(session_data)
    trace_dir = restore_trace_dir(session_data)
    mode = restore_mode(session_data)
    if not trace_file and not trace_dir:
        raise ValueError("Session file does not contain a trace_file or trace_dir.")
    if not mode:
        raise ValueError("Session file does not contain a mode.")
    saved_trace_path = trace_dir or trace_file
    assert saved_trace_path is not None
    saved_scope = _effective_trace_scope(saved_trace_path)
    if args.trace_file and _effective_trace_scope(args.trace_file) != saved_scope:
        raise ValueError("Do not combine --resume-session with a different --trace-file.")
    if getattr(args, "trace_dir", None) and _effective_trace_scope(args.trace_dir) != saved_scope:
        raise ValueError("Do not combine --resume-session with a different --trace-dir.")
    if args.mode and args.mode != mode:
        raise ValueError("Do not combine --resume-session with a different --mode.")
    if trace_dir:
        args.trace_dir = trace_dir
        args.trace_file = None
    else:
        args.trace_file = trace_file
        args.trace_dir = None
    args.mode = mode


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    session_data = None

    try:
        if args.resume_session:
            session_data = load_session(args.resume_session)
            _apply_resume_session_args(args, session_data)
        config = load_config(trace_file=args.trace_file, trace_dir=args.trace_dir, mode=args.mode)
        agent = _build_agent_from_config(config)
        if session_data is not None:
            restored_messages = restore_messages(session_data)
            if not restored_messages:
                raise ValueError("Session file does not contain saved conversation messages.")
            agent.messages = restored_messages
            agent.loop_count = restore_loop_count(session_data)
    except Exception as exc:
        print(f"Refusing to start: {exc}", file=sys.stderr)
        return 2

    session_path = create_session_path()
    resumed_from = str(Path(args.resume_session).expanduser().resolve()) if args.resume_session else None
    write_session = _bind_session_writer(
        agent=agent,
        session_path=session_path,
        args=args,
        config=config,
        resumed_from=resumed_from,
    )

    try:
        print(f"Session snapshot: {session_path}")
        write_session()
        if args.resume_session and not args.prompt and not args.interactive:
            print("Resumed session loaded. Continuing from saved context.")
            ok = _continue_agent(agent)
            write_session()
            if not ok:
                return 1
        elif args.interactive or not args.prompt:
            print(
                f"AlgoKiller harness. Trace directory loaded. Mode={args.mode}. "
                "Type 'q', 'quit', or 'exit' to stop."
            )
            try:
                session = _build_prompt_session()
            except Exception as exc:
                print(f"Refusing to start interactive prompt: {exc}", file=sys.stderr)
                return 2
            while True:
                try:
                    prompt = session.prompt("ak >> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if prompt.lower() in {"q", "quit", "exit"}:
                    break
                ok = _ask_agent(agent, prompt)
                write_session()
                print()
                if not ok:
                    break
        else:
            ok = _ask_agent(agent, " ".join(args.prompt))
            write_session()
            if not ok:
                return 1
    finally:
        try:
            write_session()
        finally:
            agent.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
