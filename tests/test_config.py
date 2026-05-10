from algokiller_harness.config import load_config


def _clear_model_env(monkeypatch):
    monkeypatch.delenv("LITELLM_PROVIDER", raising=False)
    monkeypatch.delenv("LITELLM_MODEL_NAME", raising=False)
    monkeypatch.delenv("LITELLM_MODEL", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("API_BASE", raising=False)
    monkeypatch.delenv("HARNESS_ENV_FILE", raising=False)


def test_reasoning_effort_defaults_to_medium(tmp_path, monkeypatch):
    trace_file = tmp_path / "sample.trace"
    trace_file.write_text("", encoding="utf-8")
    _clear_model_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HARNESS_REASONING_EFFORT", raising=False)

    config = load_config(trace_file=str(trace_file), mode="ciphertext")

    assert config.reasoning_effort == "medium"
    assert config.max_tokens == 99999
    assert config.model_retries == 5
    assert config.system_reinjection_interval == 50
    assert config.context_compaction_threshold_chars == 100000


def test_reasoning_effort_can_be_configured_from_env(tmp_path, monkeypatch):
    trace_file = tmp_path / "sample.trace"
    trace_file.write_text("", encoding="utf-8")
    _clear_model_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARNESS_REASONING_EFFORT", "high")

    config = load_config(trace_file=str(trace_file), mode="ciphertext")

    assert config.reasoning_effort == "high"


def test_model_defaults_to_openai_gpt5(tmp_path, monkeypatch):
    trace_file = tmp_path / "sample.trace"
    trace_file.write_text("", encoding="utf-8")
    _clear_model_env(monkeypatch)
    monkeypatch.chdir(tmp_path)

    config = load_config(trace_file=str(trace_file), mode="ciphertext")

    assert config.provider == "openai"
    assert config.model_name == "gpt-5.4"
    assert config.model == "openai/gpt-5.4"


def test_model_can_switch_to_anthropic_with_provider_and_name(tmp_path, monkeypatch):
    trace_file = tmp_path / "sample.trace"
    trace_file.write_text("", encoding="utf-8")
    _clear_model_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LITELLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LITELLM_MODEL_NAME", "claude-sonnet-4-5")

    config = load_config(trace_file=str(trace_file), mode="ciphertext")

    assert config.provider == "anthropic"
    assert config.model_name == "claude-sonnet-4-5"
    assert config.model == "anthropic/claude-sonnet-4-5"


def test_google_provider_uses_gemini_litellm_prefix(tmp_path, monkeypatch):
    trace_file = tmp_path / "sample.trace"
    trace_file.write_text("", encoding="utf-8")
    _clear_model_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LITELLM_PROVIDER", "google")
    monkeypatch.setenv("LITELLM_MODEL_NAME", "gemini-2.5-pro")

    config = load_config(trace_file=str(trace_file), mode="ciphertext")

    assert config.provider == "google"
    assert config.model_name == "gemini-2.5-pro"
    assert config.model == "gemini/gemini-2.5-pro"


def test_full_litellm_model_name_still_works(tmp_path, monkeypatch):
    trace_file = tmp_path / "sample.trace"
    trace_file.write_text("", encoding="utf-8")
    _clear_model_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LITELLM_MODEL_NAME", "openai/gpt-4.1")

    config = load_config(trace_file=str(trace_file), mode="ciphertext")

    assert config.provider == "openai"
    assert config.model_name == "gpt-4.1"
    assert config.model == "openai/gpt-4.1"


def test_openai_compatible_provider_alias_uses_openai_transport(tmp_path, monkeypatch):
    trace_file = tmp_path / "sample.trace"
    trace_file.write_text("", encoding="utf-8")
    _clear_model_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LITELLM_PROVIDER", "openai-compatible")
    monkeypatch.setenv("LITELLM_MODEL_NAME", "claude-opus-4-6")

    config = load_config(trace_file=str(trace_file), mode="ciphertext")

    assert config.provider == "openai"
    assert config.model_name == "claude-opus-4-6"
    assert config.model == "openai/claude-opus-4-6"


def test_legacy_full_litellm_model_is_ignored(tmp_path, monkeypatch):
    trace_file = tmp_path / "sample.trace"
    trace_file.write_text("", encoding="utf-8")
    _clear_model_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LITELLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LITELLM_MODEL_NAME", "claude-sonnet-4-5")
    monkeypatch.setenv("LITELLM_MODEL", "openai/gpt-4.1")

    config = load_config(trace_file=str(trace_file), mode="ciphertext")

    assert config.model == "anthropic/claude-sonnet-4-5"


def test_generic_api_key_and_base_are_loaded(tmp_path, monkeypatch):
    trace_file = tmp_path / "sample.trace"
    trace_file.write_text("", encoding="utf-8")
    _clear_model_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("API_BASE", "https://example.test/v1")

    config = load_config(trace_file=str(trace_file), mode="ciphertext")

    assert config.api_key == "test-key"
    assert config.api_base == "https://example.test/v1"


def test_custom_api_base_does_not_change_provider_transport(tmp_path, monkeypatch):
    trace_file = tmp_path / "sample.trace"
    trace_file.write_text("", encoding="utf-8")
    _clear_model_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LITELLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LITELLM_MODEL_NAME", "claude-opus-4-6")
    monkeypatch.setenv("API_BASE", "https://example.test")

    config = load_config(trace_file=str(trace_file), mode="ciphertext")

    assert config.model == "anthropic/claude-opus-4-6"
    assert config.api_base == "https://example.test"


def test_model_retries_can_be_configured_from_env(tmp_path, monkeypatch):
    trace_file = tmp_path / "sample.trace"
    trace_file.write_text("", encoding="utf-8")
    _clear_model_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARNESS_MODEL_RETRIES", "5")

    config = load_config(trace_file=str(trace_file), mode="ciphertext")

    assert config.model_retries == 5


def test_model_retries_are_at_least_one(tmp_path, monkeypatch):
    trace_file = tmp_path / "sample.trace"
    trace_file.write_text("", encoding="utf-8")
    _clear_model_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARNESS_MODEL_RETRIES", "0")

    config = load_config(trace_file=str(trace_file), mode="ciphertext")

    assert config.model_retries == 1


def test_system_reinjection_interval_can_be_configured_from_env(tmp_path, monkeypatch):
    trace_file = tmp_path / "sample.trace"
    trace_file.write_text("", encoding="utf-8")
    _clear_model_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARNESS_SYSTEM_REINJECTION_INTERVAL", "7")

    config = load_config(trace_file=str(trace_file), mode="ciphertext")

    assert config.system_reinjection_interval == 7


def test_system_reinjection_interval_is_at_least_one(tmp_path, monkeypatch):
    trace_file = tmp_path / "sample.trace"
    trace_file.write_text("", encoding="utf-8")
    _clear_model_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARNESS_SYSTEM_REINJECTION_INTERVAL", "0")

    config = load_config(trace_file=str(trace_file), mode="ciphertext")

    assert config.system_reinjection_interval == 1


def test_context_compaction_threshold_can_be_configured_from_env(tmp_path, monkeypatch):
    trace_file = tmp_path / "sample.trace"
    trace_file.write_text("", encoding="utf-8")
    _clear_model_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARNESS_CONTEXT_COMPACTION_THRESHOLD_CHARS", "12345")

    config = load_config(trace_file=str(trace_file), mode="ciphertext")

    assert config.context_compaction_threshold_chars == 12345


def test_context_compaction_threshold_zero_disables_compaction(tmp_path, monkeypatch):
    trace_file = tmp_path / "sample.trace"
    trace_file.write_text("", encoding="utf-8")
    _clear_model_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARNESS_CONTEXT_COMPACTION_THRESHOLD_CHARS", "0")

    config = load_config(trace_file=str(trace_file), mode="ciphertext")

    assert config.context_compaction_threshold_chars == 0


def test_load_config_reads_dotenv_from_current_working_directory(tmp_path, monkeypatch):
    trace_file = tmp_path / "sample.trace"
    trace_file.write_text("", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "LITELLM_PROVIDER=anthropic\n"
        "LITELLM_MODEL_NAME=claude-opus-4-6\n",
        encoding="utf-8",
    )
    _clear_model_env(monkeypatch)
    monkeypatch.chdir(tmp_path)

    config = load_config(trace_file=str(trace_file), mode="ciphertext")

    assert config.model == "anthropic/claude-opus-4-6"


def test_cwd_dotenv_overrides_stale_shell_model_env(tmp_path, monkeypatch):
    trace_file = tmp_path / "sample.trace"
    trace_file.write_text("", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "LITELLM_PROVIDER=anthropic\n"
        "LITELLM_MODEL_NAME=claude-opus-4-6\n",
        encoding="utf-8",
    )
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("LITELLM_PROVIDER", "openai")
    monkeypatch.setenv("LITELLM_MODEL_NAME", "gpt-5.4")
    monkeypatch.chdir(tmp_path)

    config = load_config(trace_file=str(trace_file), mode="ciphertext")

    assert config.model == "anthropic/claude-opus-4-6"
