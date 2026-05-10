import pytest

from algokiller_harness.cli import _format_agent_error, _route_notes, _validate_model_credentials


def test_validate_model_credentials_rejects_missing_api_key():
    with pytest.raises(RuntimeError) as exc_info:
        _validate_model_credentials("anthropic/claude-opus-4-6", api_key="")

    assert "API_KEY" in str(exc_info.value)
    assert "anthropic/claude-opus-4-6" in str(exc_info.value)


def test_validate_model_credentials_accepts_generic_api_key():
    _validate_model_credentials("openai/gpt-5.4", api_key="test-key")


def test_format_agent_error_mentions_generic_api_key():
    message = _format_agent_error(RuntimeError("authentication failed"), model="anthropic/claude-opus-4-6")

    assert "API_KEY" in message
    assert "OPENAI_API_KEY" not in message


def test_format_agent_error_explains_model_selection_error():
    message = _format_agent_error(RuntimeError("model not found: claude-opus-4-6"), model="openai/claude-opus-4-6")

    assert "Model selection failed" in message
    assert "LITELLM_MODEL_NAME" in message


def test_format_agent_error_explains_account_exhaustion():
    message = _format_agent_error(RuntimeError("All available accounts exhausted"), model="openai/gpt-5.4")

    assert "quota is exhausted" in message


def test_route_notes_explain_custom_base_keeps_provider_protocol():
    notes = _route_notes(
        provider="anthropic",
        model="anthropic/claude-opus-4-6",
        api_base="https://example.test",
    )

    assert any("anthropic protocol" in note for note in notes)
    assert any("LITELLM_PROVIDER=openai" in note for note in notes)


def test_format_agent_error_suggests_openai_provider_for_gateway_protocol_mismatch():
    message = _format_agent_error(
        RuntimeError("NotFoundError: 404 page not found"),
        model="anthropic/claude-opus-4-6",
        api_base="https://example.test",
    )

    assert "custom API_BASE does not change the protocol" in message
    assert "LITELLM_PROVIDER=openai" in message
