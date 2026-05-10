import json
import re
from pathlib import Path

from algokiller_harness.artifact_executor import ArtifactToolExecutor
from algokiller_harness.note_store import NoteStore
from algokiller_harness.tool_schemas import (
    ASK_USER_TOOL,
    RECOVERED_SOURCE_TOOL,
    TRACE_CONTEXT_TOOL,
    TRACE_SEARCH_TOOL,
)
from algokiller_harness.trace_executor import LocalTraceToolExecutor


def test_builtin_tools_are_litellm_function_tools():
    tools = [
        TRACE_SEARCH_TOOL,
        TRACE_CONTEXT_TOOL,
        ASK_USER_TOOL,
        RECOVERED_SOURCE_TOOL,
    ]

    assert all(tool["type"] == "function" for tool in tools)
    assert [tool["function"]["name"] for tool in tools] == [
        "trace_search",
        "trace_context",
        "ask_user",
        "write_recovered_source",
    ]
    assert "file" not in TRACE_SEARCH_TOOL["function"]["parameters"]["properties"]
    assert "file" not in TRACE_CONTEXT_TOOL["function"]["parameters"]["properties"]
    assert "from_line" not in TRACE_SEARCH_TOOL["function"]["parameters"]["required"]
    assert "before_line" not in TRACE_SEARCH_TOOL["function"]["parameters"]["required"]
    assert "limit" in TRACE_SEARCH_TOOL["function"]["parameters"]["required"]
    assert TRACE_SEARCH_TOOL["function"]["parameters"]["properties"]["from_line"]["minimum"] == 1
    assert TRACE_SEARCH_TOOL["function"]["parameters"]["properties"]["before_line"]["minimum"] == 1
    assert TRACE_SEARCH_TOOL["function"]["parameters"]["properties"]["limit"]["maximum"] == 100
    assert "context" not in TRACE_CONTEXT_TOOL["function"]["parameters"]["properties"]
    assert TRACE_CONTEXT_TOOL["function"]["parameters"]["required"] == ["line", "before", "after"]
    assert TRACE_CONTEXT_TOOL["function"]["parameters"]["properties"]["before"]["maximum"] == 100
    assert TRACE_CONTEXT_TOOL["function"]["parameters"]["properties"]["after"]["maximum"] == 100
    assert "Choose a search purpose before each call" in TRACE_SEARCH_TOOL["function"]["description"]
    assert "byte-reversed endian order" in TRACE_SEARCH_TOOL["function"]["description"]
    assert "2-4 distinctive" in TRACE_SEARCH_TOOL["function"]["description"]
    assert "4-byte windows" in TRACE_SEARCH_TOOL["function"]["description"]
    assert "only when the current analysis mode allows it" in ASK_USER_TOOL["function"]["description"]
    assert "trace evidence is insufficient" not in ASK_USER_TOOL["function"]["description"]


def test_trace_executor_requires_one_search_anchor_and_bounds_limit(tmp_path):
    trace_file = tmp_path / "trace.log"
    trace_file.write_text("hello\n", encoding="utf-8")
    executor = LocalTraceToolExecutor(tmp_path, trace_file)
    executor._ensure_search_bin = lambda: None

    missing_anchor = executor.execute("trace_search", {"query": "hello", "limit": 10})
    both_anchors = executor.execute("trace_search", {"query": "hello", "from_line": 1, "before_line": 2, "limit": 10})
    missing_limit = executor.execute("trace_search", {"query": "hello", "from_line": 1})
    too_large = executor.execute("trace_search", {"query": "hello", "from_line": 1, "limit": 101})

    assert '"status": "error"' in missing_anchor
    assert "exactly one of from_line or before_line is required" in missing_anchor
    assert '"status": "error"' in both_anchors
    assert "exactly one of from_line or before_line is required" in both_anchors
    assert '"status": "error"' in missing_limit
    assert "limit is required" in missing_limit
    assert '"status": "error"' in too_large
    assert "limit must be <= 100" in too_large


def test_trace_executor_before_line_uses_backward_daemon_command(tmp_path):
    trace_file = tmp_path / "trace.log"
    trace_file.write_text("hello\n", encoding="utf-8")
    executor = LocalTraceToolExecutor(tmp_path, trace_file)
    calls = []

    def fake_daemon_request(command, *, max_output_chars):
        calls.append((command, max_output_chars))
        return json.dumps({"status": "ok", "returncode": 0, "stdout": "", "stderr": "", "truncated": False})

    executor._daemon_request = fake_daemon_request

    result = executor.execute("trace_search", {"query": "hello", "before_line": 42, "limit": 2})

    assert '"status": "ok"' in result
    assert calls == [("match\t0\t42\t2\t68656c6c6f", 30000)]


def test_trace_executor_hex_search_retries_byte_reversed_when_empty(tmp_path):
    trace_file = tmp_path / "trace.log"
    trace_file.write_text("value=0x44332211\n", encoding="utf-8")
    executor = LocalTraceToolExecutor(tmp_path, trace_file)
    calls = []

    def fake_daemon_request(command, *, max_output_chars):
        calls.append((command, max_output_chars))
        query = bytes.fromhex(command.rsplit("\t", 1)[1]).decode("utf-8")
        stdout = '{"type":"match","line":1,"byte_offset":0,"text":"value=0x44332211"}\n' if query == "0x44332211" else ""
        return json.dumps({"status": "ok", "returncode": 0, "stdout": stdout, "stderr": "", "truncated": False})

    executor._daemon_request = fake_daemon_request

    result = json.loads(executor.execute("trace_search", {"query": "0x11223344", "from_line": 1, "limit": 5}))

    assert result["status"] == "ok"
    assert result["stdout"] == '{"type":"match","line":1,"byte_offset":0,"text":"value=0x44332211"}\n'
    assert "fallback_query" not in result
    assert len(calls) == 2
    assert calls[0] == ("match\t1\t0\t5\t30783131323233333434", 30000)
    assert calls[1] == ("match\t1\t0\t5\t30783434333332323131", 30000)


def test_trace_executor_hex_search_trims_leading_zero_after_reverse_misses(tmp_path):
    trace_file = tmp_path / "trace.log"
    trace_file.write_text("value=0x1123\n", encoding="utf-8")
    executor = LocalTraceToolExecutor(tmp_path, trace_file)
    queries = []

    def fake_daemon_request(command, *, max_output_chars):
        query = bytes.fromhex(command.rsplit("\t", 1)[1]).decode("utf-8")
        queries.append(query)
        stdout = '{"type":"match","line":1,"byte_offset":0,"text":"value=0x1123"}\n' if query == "0x1123" else ""
        return json.dumps({"status": "ok", "returncode": 0, "stdout": stdout, "stderr": "", "truncated": False})

    executor._daemon_request = fake_daemon_request

    result = json.loads(executor.execute("trace_search", {"query": "0x001123", "before_line": 50, "limit": 3}))

    assert result["status"] == "ok"
    assert result["stdout"] == '{"type":"match","line":1,"byte_offset":0,"text":"value=0x1123"}\n'
    assert "fallback_query" not in result
    assert queries == ["0x001123", "0x231100", "0x1123"]


def test_trace_executor_hex_search_reverses_trimmed_leading_zero_value(tmp_path):
    trace_file = tmp_path / "trace.log"
    trace_file.write_text("value=0x2211\n", encoding="utf-8")
    executor = LocalTraceToolExecutor(tmp_path, trace_file)
    queries = []

    def fake_daemon_request(command, *, max_output_chars):
        query = bytes.fromhex(command.rsplit("\t", 1)[1]).decode("utf-8")
        queries.append(query)
        stdout = '{"type":"match","line":1,"byte_offset":0,"text":"value=0x2211"}\n' if query == "0x2211" else ""
        return json.dumps({"status": "ok", "returncode": 0, "stdout": stdout, "stderr": "", "truncated": False})

    executor._daemon_request = fake_daemon_request

    result = json.loads(executor.execute("trace_search", {"query": "0x001122", "from_line": 1, "limit": 3}))

    assert result["status"] == "ok"
    assert result["stdout"] == '{"type":"match","line":1,"byte_offset":0,"text":"value=0x2211"}\n'
    assert "fallback_query" not in result
    assert queries == ["0x001122", "0x221100", "0x1122", "0x2211"]


def test_trace_executor_requires_and_bounds_context_counts(tmp_path):
    trace_file = tmp_path / "trace.log"
    trace_file.write_text("hello\n", encoding="utf-8")
    executor = LocalTraceToolExecutor(tmp_path, trace_file)
    executor._ensure_search_bin = lambda: None

    missing_before = executor.execute("trace_context", {"line": 1, "after": 1})
    missing_after = executor.execute("trace_context", {"line": 1, "before": 1})
    context_arg = executor.execute("trace_context", {"line": 1, "context": 1})
    before_too_large = executor.execute("trace_context", {"line": 1, "before": 101, "after": 1})
    after_too_large = executor.execute("trace_context", {"line": 1, "before": 1, "after": 101})

    assert '"status": "error"' in missing_before
    assert "before is required" in missing_before
    assert '"status": "error"' in missing_after
    assert "after is required" in missing_after
    assert '"status": "error"' in context_arg
    assert "context is no longer supported; use before and after" in context_arg
    assert '"status": "error"' in before_too_large
    assert "before must be <= 100" in before_too_large
    assert '"status": "error"' in after_too_large
    assert "after must be <= 100" in after_too_large


def test_artifact_executor_writes_python_source(tmp_path):
    executor = ArtifactToolExecutor(tmp_path, mode="general")

    result = executor.execute(
        "write_recovered_source",
        {"path": "recovered.py", "source": "def recovered():\n    return 1\n", "notes": "evidence"},
    )

    assert '"status": "ok"' in result
    data = json.loads(result)
    source_path = tmp_path / Path(data["source_path"]).name
    notes_path = tmp_path / Path(data["notes_path"]).name

    assert re.fullmatch(r"recovered_GENERAL_\d{8}_\d{6}\.py", source_path.name)
    assert source_path.read_text() == "def recovered():\n    return 1\n"
    assert notes_path.name == source_path.with_suffix(".notes.md").name
    assert notes_path.read_text() == "evidence"
    assert not (tmp_path / "recovered.py").exists()


def test_artifact_executor_timestamp_prevents_overwrite(tmp_path):
    executor = ArtifactToolExecutor(tmp_path, mode="ciphertext")

    first = json.loads(executor.execute("write_recovered_source", {"path": "recovered.py", "source": "first\n"}))
    second = json.loads(executor.execute("write_recovered_source", {"path": "recovered.py", "source": "second\n"}))

    assert first["source_path"] != second["source_path"]
    assert Path(first["source_path"]).read_text() == "first\n"
    assert Path(second["source_path"]).read_text() == "second\n"


def test_artifact_executor_writes_final_markdown_with_mode_timestamp(tmp_path):
    executor = ArtifactToolExecutor(tmp_path, mode="general")

    target = executor.write_final_markdown("# Result\n\nDone.\n")

    assert re.fullmatch(r"GENERAL_\d{8}_\d{6}\.md", target.name)
    assert target.parent == tmp_path
    assert target.read_text() == "# Result\n\nDone.\n"


def test_artifact_executor_reports_missing_required_arguments(tmp_path):
    executor = ArtifactToolExecutor(tmp_path)

    result = executor.execute("write_recovered_source", {"source": "print('done')\n"})

    assert '"status": "error"' in result
    assert "missing required argument: path" in result


def test_note_store_writes_strict_timestamped_note(tmp_path):
    store = NoteStore(tmp_path / "notes")

    result = store.write(
        {
            "task": "recover target ciphertext",
            "confirmed": [
                "line 1234 call func: encode(0x1000, 0x20) returns target buffer",
                "line 1240 hexdump at address 0x1000 contains target bytes",
            ],
            "high_confidence": ["The call is likely an encoding boundary, still needs source buffer validation."],
            "excluded": ["line 1400 is a later consumer, not a producer."],
            "open_questions": ["Need to identify the source buffer before line 1234."],
            "next_steps": ["Use trace_context around line 1234 before=20 after=40."],
        },
    )

    note_path = Path(result["note_path"])
    assert note_path.parent == tmp_path / "notes"
    assert re.fullmatch(r"\d{8}_\d{6}_note\.md", note_path.name)

    text = note_path.read_text(encoding="utf-8")
    assert "# AlgoKiller Progress Note" in text
    assert "- Task: recover target ciphertext" in text
    assert "## Confirmed" in text
    assert "line 1234 call func" in text
    assert "## High Confidence" in text
    assert "## Next Steps" in text


def test_note_store_accepts_register_only_confirmed_items(tmp_path):
    store = NoteStore(tmp_path / "notes")

    result = store.write(
        {
            "task": "recover target ciphertext",
            "confirmed": ["x0 contains the target buffer and mem_r follows"],
            "next_steps": ["Search for key material."],
        },
    )

    assert Path(result["note_path"]).exists()


def test_note_store_does_not_expose_tool_interface(tmp_path):
    store = NoteStore(tmp_path / "notes")

    assert not hasattr(store, "execute")


def test_note_store_rejects_missing_required_fields(tmp_path):
    store = NoteStore(tmp_path / "notes")

    try:
        store.write(
            {
                "task": "recover target ciphertext",
                "confirmed": ["line 1234 includes 0x1000"],
            },
        )
    except ValueError as exc:
        error = str(exc)
    else:
        raise AssertionError("expected missing next_steps to be rejected")

    assert "note missing required field: next_steps" in error
    assert not (tmp_path / "notes").exists()
