from types import SimpleNamespace

import json
from pathlib import Path

import litellm

from algokiller_harness.ask_user_reviewer import AskUserReviewAgent
from algokiller_harness.message_utils import (
    assistant_message,
    format_tool_output_for_console,
)
from algokiller_harness.model_client import api_kwargs
from algokiller_harness.note_compactor import NoteCompactionAgent
from algokiller_harness.tool_schemas import RECOVERED_SOURCE_TOOL, TRACE_SEARCH_TOOL
from algokiller_harness.trace_agent import TraceAgent


class StubExecutor:
    def __init__(self):
        self.calls = []

    def execute(self, name, arguments):
        self.calls.append((name, arguments))
        return "executed"

    def close(self):
        return None


class RaisingExecutor:
    def __init__(self):
        self.calls = []

    def execute(self, name, arguments):
        self.calls.append((name, arguments))
        raise KeyError("path")

    def close(self):
        return None


class StubReviewer:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    def review(self, *, messages, arguments):
        self.calls.append((messages, arguments))
        return {
            "decision": self.decision,
            "reason": "task is not complete",
            "instruction": "continue tracing with trace_search and trace_context",
        }


class FailingReviewer:
    def review(self, *, messages, arguments):
        raise RuntimeError("review unavailable")


class SequencedReviewer:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.calls = []

    def review(self, *, messages, arguments):
        self.calls.append((messages, arguments))
        decision = self.decisions.pop(0)
        return {
            "decision": decision,
            "reason": "reviewed direct assistant text",
            "instruction": "continue tracing with trace_search and trace_context",
        }


def _agent(executor, reviewer):
    return TraceAgent(
        model="test-model",
        tools=[],
        executor=executor,
        system_prompt="system",
        ask_user_reviewer=reviewer,
    )


def test_ask_user_review_rejects_premature_question_without_calling_user_executor():
    executor = StubExecutor()
    reviewer = StubReviewer("continue")
    agent = _agent(executor, reviewer)

    output = agent._execute_tool("ask_user", {"question": "是否继续追踪？"})

    assert "rejected_by_acceptance_agent" in output
    assert "continue tracing with trace_search and trace_context" in output
    assert executor.calls == []
    assert len(reviewer.calls) == 1


def test_ask_user_review_allows_real_user_question_when_needed():
    executor = StubExecutor()
    reviewer = StubReviewer("ask_user")
    agent = _agent(executor, reviewer)

    output = agent._execute_tool("ask_user", {"question": "哪一段是目标密文？"})

    assert output == "executed"
    assert executor.calls == [("ask_user", {"question": "哪一段是目标密文？"})]
    assert len(reviewer.calls) == 1


def test_ask_user_review_failure_falls_back_to_user_executor():
    executor = StubExecutor()
    agent = _agent(executor, FailingReviewer())

    output = agent._execute_tool("ask_user", {"question": "无法判断，是否继续？"})

    assert output == "executed"
    assert executor.calls == [("ask_user", {"question": "无法判断，是否继续？"})]


def test_ask_user_review_payload_only_contains_initial_prompt_and_current_question(monkeypatch):
    captured = {}

    def fake_completion(**kwargs):
        captured["messages"] = kwargs["messages"]
        captured["reasoning_effort"] = kwargs["reasoning_effort"]
        message = SimpleNamespace(content='{"decision":"continue","reason":"未完成","instruction":"继续"}')
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    reviewer = AskUserReviewAgent(model="openai/gpt-5.4", mode="ciphertext", reasoning_effort="high")

    reviewer.review(
        messages=[
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "初始密文 abcdef"},
            {"role": "assistant", "content": "large analysis context"},
            {"role": "tool", "name": "trace_context", "content": "large trace context"},
        ],
        arguments={"question": "是否继续追踪？"},
    )

    payload = captured["messages"][1]["content"]
    assert "初始密文 abcdef" in payload
    assert "是否继续追踪？" in payload
    assert "large analysis context" not in payload
    assert "large trace context" not in payload
    assert "conversation_excerpt" not in payload
    assert captured["messages"][0]["role"] == "system"
    assert "禁止输出 Markdown" in captured["messages"][0]["content"]
    assert "禁止输出代码块" in captured["messages"][0]["content"]
    assert "响应的首字符必须是 {" in captured["messages"][0]["content"]
    assert "响应的末字符必须是 }" in captured["messages"][0]["content"]
    assert captured["messages"][1]["role"] == "user"
    assert captured["reasoning_effort"] == "high"


def test_ask_user_review_parse_error_prints_raw_response(monkeypatch, capsys):
    def fake_completion(**kwargs):
        message = SimpleNamespace(content="```json\nnot json\n```")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    reviewer = AskUserReviewAgent(model="test-model", mode="ciphertext")

    review = reviewer.review(messages=[{"role": "user", "content": "task"}], arguments={"question": "continue?"})

    printed = capsys.readouterr().out
    assert review == {
        "decision": "ask_user",
        "reason": "验收 agent 未能给出可解析判断，已打印返回原文。",
        "instruction": "",
    }
    assert "ask_user_review_parse_error_raw" in printed
    assert "```json\nnot json\n```" in printed


def test_ask_user_review_fallback_parses_json_from_first_object_start(monkeypatch, capsys):
    def fake_completion(**kwargs):
        message = SimpleNamespace(
            content='analysis text before json\n{"decision":"continue","reason":"ok","instruction":"go on"}'
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    reviewer = AskUserReviewAgent(model="test-model", mode="ciphertext")

    review = reviewer.review(messages=[{"role": "user", "content": "task"}], arguments={"question": "continue?"})

    assert review == {"decision": "continue", "reason": "ok", "instruction": "go on"}
    assert capsys.readouterr().out == ""


def test_final_assistant_text_is_returned_without_internal_print(monkeypatch, capsys):
    captured = {}

    def fake_completion(**kwargs):
        captured["reasoning_effort"] = kwargs["reasoning_effort"]
        message = SimpleNamespace(content="final answer", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    executor = StubExecutor()
    agent = TraceAgent(
        model="openai/gpt-5.4",
        tools=[],
        executor=executor,
        system_prompt="system",
        reasoning_effort="xhigh",
    )

    assert agent.ask("task") == "final answer"
    assert capsys.readouterr().out == ""
    assert captured["reasoning_effort"] == "xhigh"


def test_final_assistant_text_callback_runs_before_return(monkeypatch):
    events = []

    def fake_completion(**kwargs):
        message = SimpleNamespace(content="final answer", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    def callback(text):
        events.append(("callback", text))

    monkeypatch.setattr(litellm, "completion", fake_completion)
    executor = StubExecutor()
    agent = TraceAgent(
        model="openai/gpt-5.4",
        tools=[],
        executor=executor,
        system_prompt="system",
        final_text_callback=callback,
    )

    result = agent.ask("task")
    events.append(("returned", result))

    assert result == "final answer"
    assert events == [("callback", "final answer"), ("returned", "final answer")]


def test_rejected_direct_assistant_text_does_not_write_final_callback(monkeypatch):
    calls = []
    completions = [
        SimpleNamespace(content="shall I continue?", tool_calls=None),
        SimpleNamespace(content="final answer", tool_calls=None),
    ]

    def fake_completion(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=completions.pop(0))])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    executor = StubExecutor()
    agent = TraceAgent(
        model="openai/gpt-5.4",
        tools=[],
        executor=executor,
        system_prompt="system",
        ask_user_reviewer=SequencedReviewer(["continue", "ask_user"]),
        final_text_callback=calls.append,
    )

    assert agent.ask("task") == "final answer"
    assert calls == ["final answer"]


def test_gpt5_family_request_omits_unsupported_temperature(monkeypatch):
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        message = SimpleNamespace(content="final answer", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    executor = StubExecutor()
    agent = TraceAgent(
        model="openai/gpt-5.4",
        tools=[],
        executor=executor,
        system_prompt="system",
        temperature=0,
    )

    assert agent.ask("task") == "final answer"
    assert "temperature" not in captured


def test_anthropic_request_keeps_temperature_and_reasoning_effort(monkeypatch):
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        message = SimpleNamespace(content="final answer", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    executor = StubExecutor()
    agent = TraceAgent(
        model="anthropic/claude-test",
        tools=[],
        executor=executor,
        system_prompt="system",
        temperature=0.2,
        reasoning_effort="high",
    )

    assert agent.ask("task") == "final answer"
    assert captured["temperature"] == 0.2
    assert captured["reasoning_effort"] == "high"


def test_kimi_request_omits_unsupported_temperature_and_reasoning_effort(monkeypatch):
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        message = SimpleNamespace(content="final answer", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    executor = StubExecutor()
    agent = TraceAgent(
        model="openai/kimi-k2.6",
        tools=[],
        executor=executor,
        system_prompt="system",
        temperature=0,
        reasoning_effort="medium",
    )

    assert agent.ask("task") == "final answer"
    assert "temperature" not in captured
    assert "reasoning_effort" not in captured
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}


def test_trace_agent_passes_generic_api_key_and_base(monkeypatch):
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        message = SimpleNamespace(content="final answer", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    executor = StubExecutor()
    agent = TraceAgent(
        model="anthropic/claude-test",
        tools=[],
        executor=executor,
        system_prompt="system",
        api_key="test-key",
        api_base="https://example.test/v1",
    )

    assert agent.ask("task") == "final answer"
    assert captured["api_key"] == "test-key"
    assert captured["api_base"] == "https://example.test/v1"


def test_api_kwargs_rotates_comma_separated_api_keys():
    keys = " rotate-key-a, rotate-key-b ,, rotate-key-c "

    assert api_kwargs(api_key=keys, api_base="")["api_key"] == "rotate-key-a"
    assert api_kwargs(api_key=keys, api_base="")["api_key"] == "rotate-key-b"
    assert api_kwargs(api_key=keys, api_base="")["api_key"] == "rotate-key-c"
    assert api_kwargs(api_key=keys, api_base="")["api_key"] == "rotate-key-a"


def test_trace_agent_rotates_comma_separated_api_keys_across_requests(monkeypatch):
    calls = []
    completions = [
        SimpleNamespace(
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "trace_search", "arguments": "{\"query\":\"abc\"}"},
                }
            ],
        ),
        SimpleNamespace(content="final answer", tool_calls=None),
    ]

    def fake_completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=completions.pop(0))])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    executor = StubExecutor()
    agent = TraceAgent(
        model="anthropic/claude-test",
        tools=[TRACE_SEARCH_TOOL],
        executor=executor,
        system_prompt="system",
        api_key="agent-key-a,agent-key-b",
    )

    assert agent.ask("task") == "final answer"
    assert [call["api_key"] for call in calls] == ["agent-key-a", "agent-key-b"]


def test_ask_user_review_agent_passes_generic_api_key_and_base(monkeypatch):
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        message = SimpleNamespace(content='{"decision":"continue","reason":"未完成","instruction":"继续"}')
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    reviewer = AskUserReviewAgent(
        model="anthropic/claude-test",
        mode="ciphertext",
        api_key="test-key",
        api_base="https://example.test/v1",
    )

    reviewer.review(messages=[{"role": "user", "content": "task"}], arguments={"question": "continue?"})

    assert captured["api_key"] == "test-key"
    assert captured["api_base"] == "https://example.test/v1"


def test_assistant_message_preserves_kimi_reasoning_content_for_tool_calls():
    message = SimpleNamespace(
        content="",
        reasoning_content="tool reasoning",
        provider_specific_fields={"reasoning_content": "tool reasoning", "refusal": None},
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "trace_search", "arguments": "{\"query\":\"abc\"}"},
            }
        ],
    )

    assert assistant_message(message) == {
        "role": "assistant",
        "content": "",
        "reasoning_content": "tool reasoning",
        "provider_specific_fields": {"reasoning_content": "tool reasoning", "refusal": None},
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "trace_search", "arguments": "{\"query\":\"abc\"}"},
            }
        ],
    }


def test_model_request_retries_same_model_path(monkeypatch, capsys):
    calls = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("temporary provider failure")
        message = SimpleNamespace(content="final answer", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    executor = StubExecutor()
    agent = TraceAgent(
        model="anthropic/claude-test",
        tools=[],
        executor=executor,
        system_prompt="system",
        model_retries=2,
    )

    assert agent.ask("task") == "final answer"
    assert [call["model"] for call in calls] == ["anthropic/claude-test", "anthropic/claude-test"]
    assert "model_request_retry" in capsys.readouterr().out


def test_model_request_does_not_retry_non_retryable_model_error(monkeypatch, capsys):
    calls = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("model not found: missing-model")

    monkeypatch.setattr(litellm, "completion", fake_completion)
    executor = StubExecutor()
    agent = TraceAgent(
        model="openai/missing-model",
        tools=[],
        executor=executor,
        system_prompt="system",
        model_retries=5,
    )

    try:
        agent.ask("task")
    except RuntimeError:
        pass

    assert len(calls) == 1
    assert "model_request_retry" not in capsys.readouterr().out


def test_tool_output_console_format_structures_json_lines_as_single_line_json():
    output = json.dumps(
        {
            "status": "ok",
            "stdout": json.dumps({"type": "match", "line": 1, "text": "short"}) + "\n",
        }
    )

    formatted = format_tool_output_for_console(output)

    assert "\n" not in formatted
    assert json.loads(formatted) == {
        "status": "ok",
        "stdout": [{"type": "match", "line": 1, "text": "short"}],
    }


def test_tool_output_console_format_truncates_only_stdout_when_single_line_json_is_long():
    long_text = "x" * 240
    output = json.dumps(
        {
            "status": "ok",
            "line": 123,
            "stdout": json.dumps({"type": "match", "line": 1, "text": long_text}) + "\n",
        }
    )

    formatted = format_tool_output_for_console(output)
    data = json.loads(formatted)

    assert "\n" not in formatted
    assert len(formatted) <= 200
    assert data["status"] == "ok"
    assert data["line"] == 123
    assert set(data) == {"status", "line", "stdout"}
    assert isinstance(data["stdout"], str)
    assert data["stdout"].endswith("...[truncated]")
    assert long_text[:20] in data["stdout"]
    assert long_text not in data["stdout"]


def test_unknown_tool_call_is_skipped_and_model_gets_another_turn(monkeypatch, capsys):
    captured_messages = []
    responses = [
        SimpleNamespace(
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "function": {"name": "missing_tool", "arguments": json.dumps({"value": "abc"})},
                }
            ],
        ),
        SimpleNamespace(content="final answer", tool_calls=None),
    ]

    def fake_completion(**kwargs):
        captured_messages.append(kwargs["messages"])
        return SimpleNamespace(choices=[SimpleNamespace(message=responses.pop(0))])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    executor = StubExecutor()
    agent = TraceAgent(
        model="test-model",
        tools=[TRACE_SEARCH_TOOL],
        executor=executor,
        system_prompt="system",
    )

    assert agent.ask("task") == "final answer"
    assert executor.calls == []
    assert "skipped_unknown_tool" in capsys.readouterr().out
    assert "skipped_unknown_tool" in captured_messages[1][-1]["content"]


def test_context_update_callback_runs_inside_agent_loop(monkeypatch):
    snapshots = []
    responses = [
        SimpleNamespace(
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "function": {"name": "trace_search", "arguments": json.dumps({"query": "abc"})},
                }
            ],
        ),
        SimpleNamespace(content="final answer", tool_calls=None),
    ]

    def fake_completion(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=responses.pop(0))])

    def on_context_updated(agent):
        snapshots.append(
            {
                "loop_count": agent.loop_count,
                "roles": [message["role"] for message in agent.messages],
            }
        )

    monkeypatch.setattr(litellm, "completion", fake_completion)
    executor = StubExecutor()
    agent = TraceAgent(
        model="test-model",
        tools=[TRACE_SEARCH_TOOL],
        executor=executor,
        system_prompt="system",
        context_update_callback=on_context_updated,
    )

    assert agent.ask("task") == "final answer"
    assert snapshots == [
        {"loop_count": 0, "roles": ["system", "user"]},
        {"loop_count": 1, "roles": ["system", "user", "assistant"]},
        {"loop_count": 1, "roles": ["system", "user", "assistant", "tool"]},
        {"loop_count": 2, "roles": ["system", "user", "assistant", "tool", "assistant"]},
    ]


def test_continue_conversation_uses_restored_messages_without_new_user_prompt(monkeypatch):
    captured_messages = []
    restored_messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "saved task"},
        {"role": "tool", "tool_call_id": "call_1", "name": "trace_search", "content": "saved tool result"},
    ]

    def fake_completion(**kwargs):
        captured_messages.append(kwargs["messages"])
        message = SimpleNamespace(content="final answer", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    agent = TraceAgent(
        model="test-model",
        tools=[TRACE_SEARCH_TOOL],
        executor=StubExecutor(),
        system_prompt="new default system",
    )
    agent.messages = list(restored_messages)

    assert agent.continue_conversation() == "final answer"
    assert captured_messages[0] == restored_messages
    assert [message["role"] for message in agent.messages] == ["system", "user", "tool", "assistant"]


def test_continue_conversation_resumes_pending_tool_call_before_model_request(monkeypatch):
    captured_messages = []
    restored_messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "saved task"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": "trace_search", "arguments": json.dumps({"query": "abc"})},
                }
            ],
        },
    ]

    def fake_completion(**kwargs):
        captured_messages.append(kwargs["messages"])
        message = SimpleNamespace(content="final answer", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    executor = StubExecutor()
    agent = TraceAgent(
        model="test-model",
        tools=[TRACE_SEARCH_TOOL],
        executor=executor,
        system_prompt="new default system",
    )
    agent.messages = list(restored_messages)

    assert agent.continue_conversation() == "final answer"
    assert executor.calls == [("trace_search", {"query": "abc"})]
    assert captured_messages[0][-1]["role"] == "tool"
    assert captured_messages[0][-1]["tool_call_id"] == "call_1"
    assert [message["role"] for message in agent.messages] == ["system", "user", "assistant", "tool", "assistant"]


def test_tool_execution_error_is_reported_to_model_instead_of_crashing(monkeypatch, capsys):
    captured_messages = []
    responses = [
        SimpleNamespace(
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "function": {
                        "name": "write_recovered_source",
                        "arguments": json.dumps({"source": "print('done')\n"}),
                    },
                }
            ],
        ),
        SimpleNamespace(content="final answer", tool_calls=None),
    ]

    def fake_completion(**kwargs):
        captured_messages.append(kwargs["messages"])
        return SimpleNamespace(choices=[SimpleNamespace(message=responses.pop(0))])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    executor = RaisingExecutor()
    agent = TraceAgent(
        model="test-model",
        tools=[RECOVERED_SOURCE_TOOL],
        executor=executor,
        system_prompt="system",
    )

    assert agent.ask("task") == "final answer"
    assert executor.calls == [("write_recovered_source", {"source": "print('done')\n"})]
    assert "skipped_tool_error" in capsys.readouterr().out
    assert "skipped_tool_error" in captured_messages[1][-1]["content"]


def test_direct_user_question_is_reviewed_and_rejected_before_return(monkeypatch, capsys):
    responses = [
        SimpleNamespace(content="是否继续追踪？", tool_calls=None),
        SimpleNamespace(content="final answer", tool_calls=None),
    ]

    def fake_completion(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=responses.pop(0))])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    executor = StubExecutor()
    reviewer = SequencedReviewer(["continue", "ask_user"])
    agent = TraceAgent(
        model="test-model",
        tools=[],
        executor=executor,
        system_prompt="system",
        ask_user_reviewer=reviewer,
    )

    assert agent.ask("task") == "final answer"
    printed = capsys.readouterr().out
    assert "ask_user_review" in printed
    assert "是否继续追踪？" in printed
    assert "reviewed direct assistant text" in printed
    assert len(reviewer.calls) == 2
    assert reviewer.calls[0][1]["source"] == "assistant_final_text_without_tool_call"


def test_direct_user_question_review_failure_returns_to_user(monkeypatch, capsys):
    def fake_completion(**kwargs):
        message = SimpleNamespace(content="请确认是否继续？", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    executor = StubExecutor()
    agent = TraceAgent(
        model="test-model",
        tools=[],
        executor=executor,
        system_prompt="system",
        ask_user_reviewer=FailingReviewer(),
    )

    assert agent.ask("task") == "请确认是否继续？"
    printed = capsys.readouterr().out
    assert "ask_user_review" in printed
    assert "请确认是否继续？" in printed
    assert "验收 agent 判断失败，交由用户决定" in printed


def test_system_prompt_is_reinjected_unconditionally_on_interval(monkeypatch):
    captured_messages = []
    responses = [
        SimpleNamespace(
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "function": {"name": "trace_search", "arguments": json.dumps({"query": "a"})},
                }
            ],
        ),
        SimpleNamespace(
            content="",
            tool_calls=[
                {
                    "id": "call_2",
                    "function": {"name": "trace_search", "arguments": json.dumps({"query": "b"})},
                }
            ],
        ),
        SimpleNamespace(content="final answer", tool_calls=None),
    ]

    def fake_completion(**kwargs):
        captured_messages.append(kwargs["messages"])
        return SimpleNamespace(choices=[SimpleNamespace(message=responses.pop(0))])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    executor = StubExecutor()
    agent = TraceAgent(
        model="test-model",
        tools=[TRACE_SEARCH_TOOL],
        executor=executor,
        system_prompt="full system prompt",
        system_reinjection_interval=2,
    )

    assert agent.ask("task") == "final answer"
    reinjection = captured_messages[2][-1]
    assert reinjection["role"] == "system"
    assert reinjection["content"] == (
        "系统提示刷新：不要重启任务，不要丢弃已有证据。继续当前 trace、当前 mode、当前用户目标。\n"
        "下面是完整系统约束，请重新遵守：\n"
        "full system prompt"
    )


def test_context_threshold_triggers_note_compaction_before_next_model_turn(monkeypatch):
    captured_messages = []
    responses = [
        SimpleNamespace(
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "function": {"name": "trace_search", "arguments": json.dumps({"query": "abc"})},
                }
            ],
        ),
        SimpleNamespace(content="final answer", tool_calls=None),
    ]

    class NoteCompactor:
        def __init__(self):
            self.calls = []

        def compact(self, *, messages):
            self.calls.append(messages)
            return {
                "note_path": "/repo/notes/20260501_120000_note.md",
                "content": "# AlgoKiller Progress Note\n\n## Confirmed\n- line 12 has x0=0x1\n",
            }

        def close(self):
            return None

    def fake_completion(**kwargs):
        captured_messages.append(kwargs["messages"])
        return SimpleNamespace(choices=[SimpleNamespace(message=responses.pop(0))])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    executor = StubExecutor()
    compactor = NoteCompactor()
    agent = TraceAgent(
        model="test-model",
        tools=[TRACE_SEARCH_TOOL],
        executor=executor,
        system_prompt="system",
        note_compactor=compactor,
        context_compaction_threshold_chars=1,
    )
    agent.startup_context_messages = [
        {"role": "system", "content": "system"},
        {"role": "system", "content": "bound trace"},
    ]
    agent.messages.append({"role": "user", "content": "additional constraint"})

    assert agent.ask("saved task") == "final answer"

    assert executor.calls == [("trace_search", {"query": "abc"})]
    assert len(compactor.calls) == 1
    assert [message["role"] for message in compactor.calls[0]] == ["system", "user", "user", "assistant", "tool"]
    assert [message["role"] for message in captured_messages[1]] == ["system", "system", "user"]
    assert captured_messages[1][0]["content"] == "system"
    assert captured_messages[1][1]["content"] == "bound trace"
    assert "Previous active context was cleared after automatic note compaction" in captured_messages[1][2]["content"]
    assert "Latest note path: /repo/notes/20260501_120000_note.md" in captured_messages[1][2]["content"]
    assert "Continue from this progress note" in captured_messages[1][2]["content"]
    assert "# AlgoKiller Progress Note" in captured_messages[1][2]["content"]
    assert "line 12 has x0=0x1" in captured_messages[1][2]["content"]
    assert "additional constraint" not in captured_messages[1][2]["content"]
    assert "saved task" not in captured_messages[1][2]["content"]
    assert all(message.get("role") != "tool" for message in captured_messages[1])


def test_note_compactor_fallback_parses_json_from_first_object_start(monkeypatch, tmp_path):
    message = SimpleNamespace(
        content=(
            "note draft follows\n"
            '{"task":"recover","confirmed":["line 1234 x0 contains target bytes at 0x1000"],'
            '"next_steps":["Inspect trace_context around line 1234."]}'
        )
    )

    def fake_completion(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    from algokiller_harness.note_store import NoteStore

    compactor = NoteCompactionAgent(
        model="test-model",
        note_store=NoteStore(tmp_path / "notes"),
    )

    result = compactor.compact(messages=[{"role": "user", "content": "task"}])

    assert Path(result["note_path"]).exists()
    assert "line 1234 x0 contains target bytes at 0x1000" in result["content"]


def test_note_compaction_prompt_preserves_ciphertext_algorithm_candidates():
    from algokiller_harness.agent_prompts import NOTE_COMPACTION_PROMPT

    assert "ciphertext 算法候选或排除结论" in NOTE_COMPACTION_PROMPT
    assert "候选族、匹配证据、冲突/排除理由和下一步验证项" in NOTE_COMPACTION_PROMPT
    assert "不要只保留单个算法名" in NOTE_COMPACTION_PROMPT


def test_ask_user_review_prompt_defaults_ciphertext_ambiguity_to_continue():
    from algokiller_harness.agent_prompts import ASK_USER_REVIEW_PROMPT

    assert "可复现/局部源码或伪代码" in ASK_USER_REVIEW_PROMPT
    assert "只要 initial_user_prompt 中存在一个可分析的密文本体，默认判定为 continue" in ASK_USER_REVIEW_PROMPT
    assert "必要源码" not in ASK_USER_REVIEW_PROMPT
    assert "无法可靠判断是否应该继续，判定为 ask_user" not in ASK_USER_REVIEW_PROMPT
