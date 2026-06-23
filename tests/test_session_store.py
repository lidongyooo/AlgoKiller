from argparse import Namespace
from datetime import datetime, timezone

import pytest

from algokiller_harness.cli import _apply_resume_session_args
from algokiller_harness.config import HarnessConfig
from algokiller_harness.session_store import (
    create_session_path,
    load_session,
    restore_loop_count,
    restore_messages,
    restore_mode,
    restore_trace_file,
    save_session,
)


def _config(trace_file):
    return HarnessConfig(
        env_file=None,
        provider="openai",
        model_name="gpt-5.4",
        model="openai/gpt-5.4",
        api_key="secret-key",
        api_base="https://example.test/v1",
        trace_file=trace_file,
        trace_dir=trace_file.parent,
        mode="ciphertext",
        artifacts_dir=trace_file.parent / "artifacts",
        max_tokens=99999,
        max_iterations=99999,
        model_retries=5,
        system_reinjection_interval=50,
        context_compaction_threshold_chars=100000,
        temperature=0,
        reasoning_effort="medium",
    )


def test_create_session_path_uses_datetime_json_name(tmp_path):
    path = create_session_path(
        sessions_dir=tmp_path / "sessions",
        now=datetime(2026, 4, 30, 21, 30, 15, tzinfo=timezone.utc),
    )

    assert path == tmp_path / "sessions" / "20260430_213015.json"


def test_session_snapshot_round_trips_context_without_api_key(tmp_path, monkeypatch):
    trace_file = tmp_path / "sample.trace"
    trace_file.write_text("", encoding="utf-8")
    path = tmp_path / "sessions" / "20260430_213015.json"
    args = Namespace(
        prompt=["recover", "this"],
        interactive=False,
        trace_file=str(trace_file),
        trace_dir=None,
        mode="ciphertext",
        resume_session=None,
    )
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "answer"},
    ]
    monkeypatch.setattr("sys.argv", ["run_algokiller.py", "--trace-file", str(trace_file)])

    save_session(path, args=args, config=_config(trace_file), messages=messages, loop_count=3)

    text = path.read_text(encoding="utf-8")
    data = load_session(path)
    assert "secret-key" not in text
    assert data["config"]["api_key_configured"] is True
    assert restore_trace_file(data) == str(trace_file)
    assert restore_mode(data) == "ciphertext"
    assert restore_messages(data) == messages
    assert restore_loop_count(data) == 3
    assert data["startup"]["argv"] == ["--trace-file", str(trace_file)]
    assert data["config"]["context_compaction_threshold_chars"] == 100000


def test_resume_args_are_loaded_from_session(tmp_path):
    trace_file = tmp_path / "sample.trace"
    trace_file.write_text("", encoding="utf-8")
    args = Namespace(trace_file=None, trace_dir=None, mode=None)

    _apply_resume_session_args(
        args,
        {"startup": {"trace_file": str(trace_file), "mode": "general"}},
    )

    assert args.trace_file == str(trace_file)
    assert args.mode == "general"


def test_resume_args_prefer_trace_dir_from_session(tmp_path):
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    args = Namespace(trace_file=None, trace_dir=None, mode=None)

    _apply_resume_session_args(
        args,
        {"startup": {"trace_dir": str(trace_dir), "mode": "general"}},
    )

    assert args.trace_file is None
    assert args.trace_dir == str(trace_dir)
    assert args.mode == "general"


def test_resume_args_reject_conflicting_trace_file(tmp_path):
    trace_file = tmp_path / "sample.trace"
    other_trace_dir = tmp_path / "other"
    other_trace_dir.mkdir()
    other_trace_file = other_trace_dir / "other.trace"
    trace_file.write_text("", encoding="utf-8")
    other_trace_file.write_text("", encoding="utf-8")
    args = Namespace(trace_file=str(other_trace_file), trace_dir=None, mode=None)

    with pytest.raises(ValueError, match="different --trace-file"):
        _apply_resume_session_args(
            args,
            {"startup": {"trace_file": str(trace_file), "mode": "ciphertext"}},
        )


def test_resume_args_allow_different_legacy_file_in_same_trace_dir(tmp_path):
    trace_file = tmp_path / "sample.trace"
    other_trace_file = tmp_path / "other.trace"
    trace_file.write_text("", encoding="utf-8")
    other_trace_file.write_text("", encoding="utf-8")
    args = Namespace(trace_file=str(other_trace_file), trace_dir=None, mode=None)

    _apply_resume_session_args(
        args,
        {"startup": {"trace_file": str(trace_file), "mode": "ciphertext"}},
    )

    assert args.trace_file == str(trace_file)
    assert args.trace_dir is None
    assert args.mode == "ciphertext"


def test_resume_args_allow_trace_dir_matching_legacy_trace_file_scope(tmp_path):
    trace_file = tmp_path / "sample.trace"
    trace_file.write_text("", encoding="utf-8")
    args = Namespace(trace_file=None, trace_dir=str(tmp_path), mode=None)

    _apply_resume_session_args(
        args,
        {"startup": {"trace_file": str(trace_file), "mode": "ciphertext"}},
    )

    assert args.trace_file == str(trace_file)
    assert args.trace_dir is None
    assert args.mode == "ciphertext"


def test_resume_args_reject_conflicting_trace_dir(tmp_path):
    trace_dir = tmp_path / "traces"
    other_trace_dir = tmp_path / "other_traces"
    trace_dir.mkdir()
    other_trace_dir.mkdir()
    args = Namespace(trace_file=None, trace_dir=str(other_trace_dir), mode=None)

    with pytest.raises(ValueError, match="different --trace-dir"):
        _apply_resume_session_args(
            args,
            {"startup": {"trace_dir": str(trace_dir), "mode": "ciphertext"}},
        )
