"""Configuration models for WorkFlowy MCP server."""

from __future__ import annotations

import logging
from collections.abc import Mapping

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings


# @beacon[
#   id=config-models@api-configuration,
#   role=APIConfiguration,
#   slice_labels=nexus--config,nexus-loading-flow,
#   kind=ast,
#   comment=Validated API client configuration model used after server settings are resolved,
# ]
class APIConfiguration(BaseModel):
    """Server configuration for WorkFlowy API access."""

    api_key: SecretStr = Field(..., description="WorkFlowy API authentication key")
    base_url: str = Field("https://workflowy.com/api/v1", description="API base URL")
    timeout: int = Field(30, gt=0, description="Request timeout in seconds")
    max_retries: int = Field(3, ge=0, description="Maximum retry attempts")

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, v: SecretStr) -> SecretStr:
        """Ensure API key is not empty."""
        if not v or not v.get_secret_value().strip():
            raise ValueError("API key must be non-empty")
        return v

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, v: str) -> str:
        """Ensure base URL is HTTPS."""
        if not v.startswith("https://"):
            raise ValueError("API base URL must use HTTPS")
        return v.rstrip("/")

    @field_validator("timeout")
    @classmethod
    def validate_timeout(cls, v: int) -> int:
        """Ensure timeout is positive."""
        if v <= 0:
            raise ValueError("Timeout must be positive")
        return v

    @field_validator("max_retries")
    @classmethod
    def validate_max_retries(cls, v: int) -> int:
        """Ensure max_retries is non-negative."""
        if v < 0:
            raise ValueError("Max retries must be non-negative")
        return v


# @beacon[
#   id=config-models@paths-configuration,
#   role=PathsConfiguration,
#   slice_labels=nexus--config,nexus-loading-flow,
#   kind=ast,
#   comment=Optional runtime filesystem paths loaded from workflowy_nexus config JSON,
# ]
class PathsConfiguration(BaseModel):
    """Optional runtime filesystem paths and local tool locations."""

    nexus_runs_base: str | None = Field(None, alias="nexusRunsBase")
    cartographer_jobs_dir: str | None = Field(None, alias="cartographerJobsDir")
    cartographer_file_refresh_dir: str | None = Field(None, alias="cartographerFileRefreshDir")
    windsurf_exe_candidates: list[str] = Field(default_factory=list, alias="windsurfExeCandidates")

    # GLIMPSE cache / staging paths (added 2025-11-20 config expansion)
    glimpse_agent_workspace: str | None = Field(None, alias="glimpseAgentWorkspace")
    glimpse_cache_source: str | None = Field(None, alias="glimpseCacheSource")
    glimpse_wsl_repo_dir: str | None = Field(None, alias="glimpseWslRepoDir")
    glimpse_staging_subdir: str | None = Field(None, alias="glimpseStagingSubdir")

    # WorkFlowy Electron app introspection paths
    workflowy_app_asar: str | None = Field(None, alias="workflowyAppAsar")
    workflowy_electron_cache_dirs: list[str] = Field(
        default_factory=list, alias="workflowyElectronCacheDirs"
    )

    model_config = {
        "populate_by_name": True,
        "validate_by_name": True,
        "validate_by_alias": True,
        "extra": "forbid",
    }


# @beacon[
#   id=config-models@websocket-configuration,
#   role=WebSocketConfiguration,
#   slice_labels=nexus--config,nexus-loading-flow,
#   kind=ast,
#   comment=WebSocket host and port settings for server-extension communication,
# ]
class WebSocketConfiguration(BaseModel):
    """WebSocket settings for the Workflowy GLIMPSE bridge."""

    host: str = Field("localhost")
    port: int = Field(8765, gt=0, le=65535)

    model_config = {
        "populate_by_name": True,
        "validate_by_name": True,
        "validate_by_alias": True,
        "extra": "forbid",
    }


# @beacon[
#   id=config-models@timing-configuration,
#   role=TimingConfiguration,
#   slice_labels=nexus--config,nexus-loading-flow,
#   kind=ast,
#   comment=Timing and polling defaults loaded from workflowy_nexus config JSON,
# ]
class TimingConfiguration(BaseModel):
    """Timing defaults for extension reconnect and polling behavior."""

    websocket_reconnect_delay_ms: int = Field(3000, alias="websocketReconnectDelayMs", ge=0)
    carto_jobs_poll_interval_ms: int = Field(1000, alias="cartoJobsPollIntervalMs", ge=0)
    # S02: best-effort settle delay between refresh-cut capture and the
    # /nodes-export request, giving Workflowy's server time to persist
    # just-committed GUI edits so the export contains the edits whose dirt
    # generations the cut will clear.  Cut-first ordering is load-bearing:
    # signals arriving during the delay post-date the cut and survive.
    refresh_settle_seconds: float = Field(2.0, alias="refreshSettleSeconds", ge=0, le=30)

    model_config = {
        "populate_by_name": True,
        "validate_by_name": True,
        "validate_by_alias": True,
        "extra": "forbid",
    }


# @beacon[
#   id=config-models@retention-configuration,
#   role=RetentionConfiguration,
#   slice_labels=nexus--config,nexus-loading-flow,
#   kind=ast,
#   comment=Retention and garbage-collection settings loaded from workflowy_nexus config JSON,
# ]
class RetentionConfiguration(BaseModel):
    """Retention settings for CARTO/F12 artifacts."""

    carto_job_max_age_seconds: int = Field(3600, alias="cartoJobMaxAgeSeconds", ge=0)

    model_config = {
        "populate_by_name": True,
        "validate_by_name": True,
        "validate_by_alias": True,
        "extra": "forbid",
    }


# @beacon[
#   id=carto-governance@config-models,
#   slice_labels=ra-carto-jobs,nexus--config,nexus-loading-flow,
#   kind=ast,
#   comment=Closed configuration for the in-process write schedule and persisted breaker/Retry-After; pacing and validation stay unchanged while the default caller window is lengthened.,
# ]
class WriteGovernorConfiguration(BaseModel):
    """Account-scoped in-process scheduler and durable recovery settings."""

    interval_seconds: float = Field(1.0, gt=0)
    max_wait_before_refusal_seconds: float = Field(3600.0, ge=0)
    breaker_initial_cooldown_seconds: float = Field(900.0, gt=0)
    breaker_max_cooldown_seconds: float = Field(7200.0, gt=0)
    probe_lease_seconds: float = Field(120.0, gt=0)
    retry_after_fallback_seconds: float = Field(60.0, ge=0)

    model_config = {"extra": "forbid"}


class AdmissionConfiguration(BaseModel):
    """Configured post-terminal memory cooldown; no hold-lifetime settings."""

    cooldown_seconds: float = Field(60.0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _discard_retired_timing_fields(cls, value):
        """Read-discard exactly the three retired timing keys, without migration."""
        if isinstance(value, Mapping):
            value = dict(value)
            for key in ("start_grace_seconds", "spawn_grace_seconds", "orphan_grace_seconds"):
                value.pop(key, None)
        return value

    model_config = {"extra": "forbid"}


class GovernanceConfiguration(BaseModel):
    """Shared governance mutex settings and Cartographer deletion thresholds."""

    mutex_wait_seconds: float = Field(10.0, gt=0)
    # U5 fail-closed refresh guards: deletion-count thresholds armed on the
    # Cartographer refresh paths.  A folder refresh whose missing-FILE sweep
    # would delete more than carto_folder_file_delete_threshold nodes refuses
    # before any write; a per-file reconcile planning more than
    # carto_reconcile_delete_threshold delete-roots aborts through the
    # reconcile engine's existing threshold machinery.  Both are overridable
    # ONLY by an explicit per-run force (beacon.map.refresh force=True / the
    # F12+1 popup override row) — never by configuration.  Conservative
    # defaults: the folder sweep cap is 10 (the unmounted-root incident
    # deleted 93 FILE nodes; ordinary temp-file churn is 1-3) and the
    # reconcile cap is 100 delete-roots (legitimate single-file refactors
    # stay green; the emptied-file case refuses).
    carto_folder_file_delete_threshold: int = Field(
        10, alias="cartoFolderFileDeleteThreshold", ge=0
    )
    carto_reconcile_delete_threshold: int = Field(
        100, alias="cartoReconcileDeleteThreshold", ge=0
    )

    @model_validator(mode="before")
    @classmethod
    def _discard_retired_lease_fields(cls, value):
        """Warn and read-discard only the four retired lease keys, in either spelling."""
        if isinstance(value, Mapping):
            value = dict(value)
            for key in (
                "mutation_lease_wait_seconds", "mutationLeaseWaitSeconds",
                "mutation_lease_poll_seconds", "mutationLeasePollSeconds",
                "mutation_lease_retry_after_seconds", "mutationLeaseRetryAfterSeconds",
                "carto_mutation_lease_retry_after_seconds", "cartoMutationLeaseRetryAfterSeconds",
            ):
                if key in value:
                    value.pop(key)
                    logging.getLogger(__name__).warning(
                        "Ignoring retired governance configuration key %s", key
                    )
        return value

    model_config = {"extra": "forbid"}


# @beacon-close[
#   id=carto-governance@config-models,
# ]


# @beacon[
#   id=config-models@coherence-configuration,
#   slice_labels=nexus--config,nexus-loading-flow,ra-coherence,
#   kind=ast,
#   comment=S04 §9.1 coherence checkpoint configuration: count/byte rotation thresholds (active replay tail only), oversized-record diagnostic threshold (diagnostic only — a valid oversized transaction is never rejected), and the bounded head catch-up pass budget. Tests inject tiny thresholds; production never waits on them.,
# ]
class CoherenceConfiguration(BaseModel):
    """S04 coherence checkpoint rotation settings."""

    checkpoint_record_threshold: int = Field(
        500, alias="checkpointRecordThreshold", ge=1
    )
    checkpoint_byte_threshold: int = Field(
        67_108_864, alias="checkpointByteThreshold", ge=1
    )
    journal_record_warning_bytes: int = Field(
        8_388_608, alias="journalRecordWarningBytes", ge=1
    )
    checkpoint_max_catchup_passes: int = Field(
        3, alias="checkpointMaxCatchupPasses", ge=1, le=20
    )

    model_config = {
        "populate_by_name": True,
        "validate_by_name": True,
        "validate_by_alias": True,
        "extra": "forbid",
    }


# @beacon[
#   id=config-models@server-config,
#   role=ServerConfig,
#   slice_labels=nexus--config,nexus-loading-flow,
#   kind=ast,
#   comment=Merged runtime configuration model populated from config file plus environment overrides,
# ]
class ServerConfig(BaseSettings):
    """Resolved runtime configuration for the Workflowy MCP server."""

    schema_version: int = Field(1, alias="schemaVersion")
    mcp_profile: str = Field("consolidated", alias="mcpProfile")

    # WorkFlowy API settings
    workflowy_api_key: SecretStr = Field(
        ..., description="WorkFlowy API key", alias="WORKFLOWY_API_KEY"
    )
    workflowy_api_url: str = Field(
        "https://workflowy.com/api/v1",
        description="WorkFlowy API base URL",
        alias="WORKFLOWY_API_URL",
    )
    workflowy_timeout: int = Field(
        30, description="API request timeout in seconds", alias="WORKFLOWY_TIMEOUT"
    )
    workflowy_max_retries: int = Field(
        3, description="Maximum retry attempts", alias="WORKFLOWY_MAX_RETRIES"
    )

    # WORKFLOWY_V1_ONLY is applied by the central environment-override row.
    v1_only: bool = Field(False, alias="v1_only", description="Latch public-v1-only create placement on first selection for this daemon life")

    # One lengthenable budget for the whole typed-region acquisition and its
    # one replacement, never a hold-duration limit or an ownership expiry.
    mutation_lock_acquire_timeout_seconds: float = Field(
        60.0,
        description=(
            "Single total typed-region acquisition wait budget in seconds, "
            "including one re-acquisition; lengthenable and never a hold-duration "
            "limit. Exhaustion refuses the call with typed retry guidance"
        ),
        alias="mutationLockAcquireTimeoutSeconds",
        gt=0,
    )
    mutation_lock_long_lived_retry_after_seconds: float = Field(
        60.0,
        description=(
            "Positive retry advice when a declared long-lived holder or live CARTO "
            "claim has no useful completion estimate; never expires, releases or kills a holder"
        ),
        alias="mutationLockLongLivedRetryAfterSeconds",
        gt=0,
    )

    # Server settings
    debug: bool = Field(False, description="Enable debug mode", alias="DEBUG")
    log_level: str = Field("INFO", description="Logging level", alias="LOG_LEVEL")

    # Optional external JSON-config sections
    paths: PathsConfiguration = Field(default_factory=PathsConfiguration)
    websocket: WebSocketConfiguration = Field(default_factory=WebSocketConfiguration)
    timing: TimingConfiguration = Field(default_factory=TimingConfiguration)
    retention: RetentionConfiguration = Field(default_factory=RetentionConfiguration)
    write_governor: WriteGovernorConfiguration = Field(default_factory=WriteGovernorConfiguration)
    admission: AdmissionConfiguration = Field(default_factory=AdmissionConfiguration)
    governance: GovernanceConfiguration = Field(default_factory=GovernanceConfiguration)
    coherence: CoherenceConfiguration = Field(default_factory=CoherenceConfiguration)

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "populate_by_name": True,
        "validate_by_name": True,
        "validate_by_alias": True,
        "extra": "forbid",
    }

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, v: int) -> int:
        """Only schema version 1 is currently supported."""
        if v != 1:
            raise ValueError("Unsupported config schemaVersion; expected 1")
        return v

    @field_validator("mcp_profile")
    @classmethod
    def validate_mcp_profile(cls, v: str) -> str:
        """Normalize and validate the import-time MCP registration profile."""
        norm = str(v).strip().lower()
        if norm not in {"legacy", "consolidated"}:
            raise ValueError("mcpProfile must be one of legacy, consolidated")
        return norm

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Normalize logging level names."""
        norm = str(v).strip().upper()
        if norm not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL")
        return norm

    # @beacon[
    #   id=config-models@server-config-to-api-config,
    #   role=ServerConfig.get_api_config,
    #   slice_labels=nexus--config,nexus-loading-flow,
    #   kind=ast,
    #   comment=Convert loaded server settings into the APIConfiguration consumed by WorkFlowyClient,
    # ]
    def get_api_config(self) -> APIConfiguration:
        """Convert to APIConfiguration for the API client."""
        return APIConfiguration(
            api_key=self.workflowy_api_key,
            base_url=self.workflowy_api_url,
            timeout=self.workflowy_timeout,
            max_retries=self.workflowy_max_retries,
        )
