"""W-2: retired lease settings warn and disappear; the example still loads."""

import json
import logging
from pathlib import Path

import pytest

from workflowy_mcp.models.config import GovernanceConfiguration, ServerConfig


RETIRED_KEYS = (
    "mutation_lease_wait_seconds",
    "mutationLeaseWaitSeconds",
    "mutation_lease_poll_seconds",
    "mutationLeasePollSeconds",
    "mutation_lease_retry_after_seconds",
    "mutationLeaseRetryAfterSeconds",
    "carto_mutation_lease_retry_after_seconds",
    "cartoMutationLeaseRetryAfterSeconds",
)


@pytest.mark.parametrize("key", RETIRED_KEYS)
def test_retired_governance_key_warns_and_is_discarded(key, caplog):
    # No validation or migration of a retired value, and no caller-data mutation.
    payload = {key: {"retired": "any value"}, "mutex_wait_seconds": 7.0}
    with caplog.at_level(logging.WARNING, logger="workflowy_mcp.models.config"):
        config = GovernanceConfiguration.model_validate(payload)
    assert config.mutex_wait_seconds == 7.0
    assert not hasattr(config, key)
    assert key not in config.model_dump()
    assert key in payload
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert key in warnings[0].getMessage()


def test_shipped_example_loads_through_server_config():
    path = Path(__file__).resolve().parents[1] / "workflowy_nexus.config.example.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert not set(RETIRED_KEYS).intersection(payload["governance"])
    config = ServerConfig.model_validate({**payload, "WORKFLOWY_API_KEY": "test-key"})
    assert config.governance.carto_folder_file_delete_threshold == 10
    assert config.governance.carto_reconcile_delete_threshold == 100


def test_invented_governance_key_still_rejected():
    with pytest.raises(ValueError, match="invented_key"):
        GovernanceConfiguration.model_validate({"invented_key": 1})
