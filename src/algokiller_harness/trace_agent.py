from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .ask_user_reviewer import AskUserReviewAgent
from .message_utils import (
    assistant_message,
    format_tool_output_for_console,
    known_tool_names,
    system_reinjection_message,
    tool_call_id_or_placeholder,
    tool_call_name_and_args,
)
from .model_client import (
    api_kwargs,
    clean_jsonable,
    clean_text,
    completion_with_retries,
    extra_body_kwargs,
    message_text,
    reasoning_effort_kwargs,
    temperature_kwargs,
)
from .note_compactor import NoteCompactionAgent
from .tool_protocol import ToolExecutor


class TraceAgent:
    def __init__(
        self,
        *,
        model: str,
        tools: list[dict[str, Any]],
        executor: ToolExecutor,
        system_prompt: str,
        ask_user_reviewer: AskUserReviewAgent | None = None,
        max_tokens: int = 99999,
        max_iterations: int = 999999999,
        model_retries: int = 5,
        system_reinjection_interval: int = 20,
        temperature: float = 0,
        reasoning_effort: str = "medium",
        api_key: str = "",
        api_base: str = "",
        context_update_callback: Callable[["TraceAgent"], None] | None = None,
        final_text_callback: Callable[[str], None] | None = None,
        note_compactor: NoteCompactionAgent | None = None,
        context_compaction_threshold_chars: int = 500000,
    ):
        self.model = model
        self.tools = tools
        self.executor = executor
        self.ask_user_reviewer = ask_user_reviewer
        self.max_tokens = max_tokens
        self.max_iterations = max_iterations
        self.model_retries = model_retries
        self.system_prompt = system_prompt
        self.system_reinjection_interval = max(1, system_reinjection_interval)
        self.loop_count = 0
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort
        self.api_key = api_key
        self.api_base = api_base
        self.context_update_callback = context_update_callback
        self.final_text_callback = final_text_callback
        self.note_compactor = note_compactor
        self.context_compaction_threshold_chars = max(0, context_compaction_threshold_chars)
        self.compaction_retry_after_loop = 0
        self.known_tool_names = known_tool_names(tools)
        self.startup_context_messages: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

    def ask(self, prompt: str) -> str:
        self._append_message({"role": "user", "content": clean_text(prompt)})
        return self.continue_conversation()

    def continue_conversation(self) -> str:
        for _ in range(self.max_iterations):
            if self._resume_pending_tool_calls():
                continue
            self._maybe_reinject_system_prompt()
            response = completion_with_retries(
                max_attempts=self.model_retries,
                model=self.model,
                messages=clean_jsonable(self.messages),
                tools=clean_jsonable(self.tools),
                tool_choice="auto",
                max_tokens=self.max_tokens,
                timeout=9999,
                **api_kwargs(api_key=self.api_key, api_base=self.api_base),
                **temperature_kwargs(model=self.model, temperature=self.temperature),
                **reasoning_effort_kwargs(model=self.model, reasoning_effort=self.reasoning_effort),
                **extra_body_kwargs(model=self.model),
            )
            self.loop_count += 1
            message = response.choices[0].message
            reasoning_content = getattr(message, "reasoning_content", None) or ""
            self._append_message(clean_jsonable(assistant_message(message)))
            assistant_text = clean_text(message_text(message)).strip()

            tool_calls = getattr(message, "tool_calls", None)
            if not tool_calls:
                # if self._review_direct_assistant_text(assistant_text):
                #     continue
                self._notify_final_text(assistant_text)
                return assistant_text

            if assistant_text != "":
                print(assistant_text)
                print()
                print()
            elif reasoning_content:
                print(reasoning_content)
                print()
                print()

            self._execute_and_append_tool_calls(list(tool_calls))
            self._maybe_compact_context()

        return "Stopped: reached HARNESS_MAX_ITERATIONS before the model produced a final answer."

    def _resume_pending_tool_calls(self) -> bool:
        for index in range(len(self.messages) - 1, -1, -1):
            message = self.messages[index]
            if message.get("role") != "assistant":
                continue
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list) or not tool_calls:
                return False
            following_messages = self.messages[index + 1 :]
            if any(item.get("role") != "tool" for item in following_messages):
                return False
            completed_ids = {
                str(item.get("tool_call_id"))
                for item in following_messages
                if item.get("role") == "tool" and item.get("tool_call_id")
            }
            pending_tool_calls = [
                tool_call
                for tool_call in tool_calls
                if tool_call_id_or_placeholder(tool_call) not in completed_ids
            ]
            if not pending_tool_calls:
                return False
            self._execute_and_append_tool_calls(pending_tool_calls)
            return True
        return False

    def _execute_and_append_tool_calls(self, tool_calls: list[Any]) -> None:
        for tool_call in tool_calls:
            name, arguments, output = self._execute_tool_call_safely(tool_call)
            print(f"> {name}({json.dumps(arguments, ensure_ascii=False)})")
            print(format_tool_output_for_console(output))
            print()
            print()
            self._append_message(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id_or_placeholder(tool_call),
                    "name": name,
                    "content": output,
                }
            )

    def _append_message(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        self._notify_context_updated()

    def _replace_messages(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages
        self._notify_context_updated()

    def _notify_context_updated(self) -> None:
        if self.context_update_callback is not None:
            self.context_update_callback(self)

    def _notify_final_text(self, assistant_text: str) -> None:
        if self.final_text_callback is not None:
            self.final_text_callback(assistant_text)

    def _maybe_reinject_system_prompt(self) -> None:
        if self.loop_count == 0 or self.loop_count % self.system_reinjection_interval != 0:
            return
        self._append_message(
            {
                "role": "system",
                "content": system_reinjection_message(self.system_prompt),
            }
        )

    def _tool_error_result(self, *, status: str, error: str, instruction: str) -> str:
        return json.dumps(
            {
                "status": status,
                "error": error,
                "instruction": instruction,
            },
            ensure_ascii=False,
        )

    def _startup_context_messages(self) -> list[dict[str, Any]]:
        if self.startup_context_messages:
            return [dict(message) for message in self.startup_context_messages]
        return [{"role": "system", "content": self.system_prompt}]

    def _context_size_chars(self) -> int:
        return len(json.dumps(clean_jsonable(self.messages), ensure_ascii=False))

    def _maybe_compact_context(self) -> None:
        if self.note_compactor is None or self.context_compaction_threshold_chars <= 0:
            return
        if self.loop_count < self.compaction_retry_after_loop:
            return
        if self._context_size_chars() < self.context_compaction_threshold_chars:
            return
        try:
            note = self.note_compactor.compact(messages=clean_jsonable(self.messages))
        except Exception as exc:
            self.compaction_retry_after_loop = self.loop_count + 3
            return
        self.compaction_retry_after_loop = 0
        self._rebuild_context_after_compaction(note_path=note["note_path"], note_content=note["content"])

    def _rebuild_context_after_compaction(self, *, note_path: str, note_content: str) -> None:
        note_task_message = {
            "role": "user",
            "content": (
                "Previous active context was cleared after automatic note compaction.\n"
                f"Latest note path: {note_path}\n"
                "Continue from this progress note. Before relying on confirmed facts, verify the key anchors with trace_context.\n\n"
                f"{note_content.strip()}"
            ),
        }
        self._replace_messages([*self._startup_context_messages(), note_task_message])

    def _execute_tool_call_safely(self, tool_call: Any) -> tuple[str, dict[str, Any], str]:
        try:
            name, arguments = tool_call_name_and_args(tool_call)
        except Exception as exc:
            return (
                "invalid_tool_call",
                {},
                self._tool_error_result(
                    status="skipped_invalid_tool_call",
                    error=str(exc),
                    instruction=(
                        "工具调用格式无效。不要停止，也不要询问用户；请基于上一轮任务重新选择可用工具继续分析。"
                    ),
                ),
            )

        if name not in self.known_tool_names:
            return (
                name,
                arguments,
                self._tool_error_result(
                    status="skipped_unknown_tool",
                    error=f"Unknown tool: {name}",
                    instruction=(
                        "该工具不存在，已跳过。不要停止，也不要询问用户；请只使用 trace_files、"
                        "trace_all_search、trace_search、trace_context、ask_user、write_recovered_source 重新推进上一轮任务。"
                    ),
                ),
            )

        try:
            return name, arguments, clean_text(self._execute_tool(name, arguments))
        except Exception as exc:
            return (
                name,
                arguments,
                self._tool_error_result(
                    status="skipped_tool_error",
                    error=str(exc),
                    instruction=(
                        "工具调用失败，已跳过。不要停止，也不要询问用户；请修正参数或改用可用工具，"
                        "继续上一轮任务。"
                    ),
                ),
            )

    def _review_direct_assistant_text(self, assistant_text: str) -> bool:
        if not assistant_text or self.ask_user_reviewer is None:
            return False
        arguments = {
            "question": assistant_text,
            "source": "assistant_final_text_without_tool_call",
        }
        try:
            review = self.ask_user_reviewer.review(messages=self.messages, arguments=arguments)
        except Exception as exc:
            print(f"> ask_user_review({json.dumps(arguments, ensure_ascii=False)})")
            print(
                json.dumps(
                    {
                        "decision": "ask_user",
                        "reason": f"验收 agent 判断失败，交由用户决定：{exc}",
                        "instruction": "",
                    },
                    ensure_ascii=False,
                )
            )
            return False

        print(f"> ask_user_review({json.dumps(arguments, ensure_ascii=False)})")
        print(json.dumps(review, ensure_ascii=False))
        if review["decision"] != "continue":
            return False

        self._append_message(
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "status": "direct_user_question_rejected_by_acceptance_agent",
                        "reason": review["reason"],
                        "instruction": review["instruction"]
                        or (
                            "用户任务尚未完成。不要直接询问用户是否继续；继续使用 trace_files、trace_all_search、"
                            "trace_search、trace_context 和必要的 artifact 工具推进分析。"
                        ),
                    },
                    ensure_ascii=False,
                ),
            }
        )
        return True

    def _execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "ask_user" and self.ask_user_reviewer is not None:
            try:
                review = self.ask_user_reviewer.review(messages=self.messages, arguments=arguments)
            except Exception:
                return self.executor.execute(name, arguments)
            if review["decision"] == "continue":
                return json.dumps(
                    {
                        "status": "rejected_by_acceptance_agent",
                        "decision": "continue",
                        "reason": review["reason"],
                        "instruction": review["instruction"] or ("继续推进。"),
                    },
                    ensure_ascii=False,
                )
        return self.executor.execute(name, arguments)

    def close(self) -> None:
        if self.note_compactor is not None:
            self.note_compactor.close()
        self.executor.close()
