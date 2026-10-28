from __future__ import annotations

import json

import pytest

from workflowy_mcp import config
from workflowy_mcp.models.config import ServerConfig


def test_registration_profile_defaults_without_api_credentials(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKFLOWY_NEXUS_CONFIG", raising=False)
    monkeypatch.delenv("WORKFLOWY_MCP_PROFILE", raising=False)
    monkeypatch.delenv("WORKFLOWY_API_KEY", raising=False)
    assert config.resolve_mcp_registration_profile() == "consolidated"


def test_blank_profile_env_is_treated_as_unset(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKFLOWY_NEXUS_CONFIG", raising=False)
    monkeypatch.setenv("WORKFLOWY_MCP_PROFILE", "   ")
    monkeypatch.delenv("WORKFLOWY_API_KEY", raising=False)
    assert config.resolve_mcp_registration_profile() == "consolidated"


def test_blank_profile_env_falls_through_to_raw_config(monkeypatch, tmp_path):
    config_path = tmp_path / "profile.json"
    config_path.write_text(json.dumps({"mcpProfile": "legacy"}), encoding="utf-8")
    monkeypatch.setenv("WORKFLOWY_NEXUS_CONFIG", str(config_path))
    monkeypatch.setenv("WORKFLOWY_MCP_PROFILE", "")
    monkeypatch.delenv("WORKFLOWY_API_KEY", raising=False)
    assert config.resolve_mcp_registration_profile() == "legacy"


def test_raw_config_profile_and_environment_precedence(monkeypatch, tmp_path):
    config_path = tmp_path / "profile.json"
    config_path.write_text(
        json.dumps({"mcpProfile": "consolidated"}), encoding="utf-8"
    )
    monkeypatch.setenv("WORKFLOWY_NEXUS_CONFIG", str(config_path))
    monkeypatch.delenv("WORKFLOWY_MCP_PROFILE", raising=False)
    monkeypatch.delenv("WORKFLOWY_API_KEY", raising=False)

    raw, meta = config._discover_raw_config()
    assert raw == {"mcpProfile": "consolidated"}
    assert meta["config_path"] == str(config_path.resolve())
    assert config.resolve_mcp_registration_profile() == "consolidated"

    monkeypatch.setenv("WORKFLOWY_MCP_PROFILE", " LEGACY ")
    assert config.resolve_mcp_registration_profile() == "legacy"


def test_invalid_profile_fails_loudly(monkeypatch):
    monkeypatch.setenv("WORKFLOWY_MCP_PROFILE", "hybrid")
    with pytest.raises(ValueError, match="valid values: legacy, consolidated"):
        config.resolve_mcp_registration_profile()


def test_full_config_env_rail_and_model_normalization(monkeypatch):
    monkeypatch.setenv("WORKFLOWY_MCP_PROFILE", " CONSOLIDATED ")
    merged = config._merge_env_overrides({"mcpProfile": "legacy"})
    assert merged["mcpProfile"] == " CONSOLIDATED "
    model = ServerConfig.model_validate(
        {"WORKFLOWY_API_KEY": "test-key", **merged}
    )
    assert model.mcp_profile == "consolidated"


def test_full_config_env_rail_skips_blank_values(monkeypatch):
    monkeypatch.setenv("WORKFLOWY_MCP_PROFILE", "")
    merged = config._merge_env_overrides({"mcpProfile": "legacy"})
    assert merged["mcpProfile"] == "legacy"
