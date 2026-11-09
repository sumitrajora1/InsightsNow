from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EDQ_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )
    environment: Literal["development", "test", "production"] = "development"
    auth_mode: Literal["development", "oidc", "trino_ldaps"] = "development"
    authorization_provider: Literal["local", "trino_ranger"] = "local"
    ldaps_identity_namespace: str = ""
    login_failure_limit: int = 5
    login_failure_window_seconds: int = 60
    database_url: str = "sqlite:///./.runtime/sessions.db"
    policy_path: Path = ROOT / "config/policy.json"
    metadata_provider: Literal["fixture", "openmetadata"] = "fixture"
    metadata_fixture: Path = ROOT / "fixtures/metadata.json"
    llm_provider: Literal["mock", "bedrock"] = "mock"
    executor: Literal["sqlite_demo", "trino"] = "sqlite_demo"
    demo_data_path: Path = ROOT / ".runtime/lending.db"
    max_rows: int = 200
    max_result_bytes: int = 200_000
    max_cell_chars: int = 2000
    query_timeout_seconds: float = 20
    dependency_timeout_seconds: float = 10
    max_concurrent_jobs: int = 4
    max_user_jobs: int = 2
    max_session_history: int = 20
    max_metadata_chars: int = 24000
    retention_days: int = 30
    audit_store_text: bool = False
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    oidc_roles_claim: str = "roles"
    oidc_username_claim: str = "preferred_username"
    om_url: str = ""
    om_token: SecretStr = SecretStr("")
    om_expected_version: str = ""
    om_api_prefix: str = "/api/v1"
    om_table_fields: str = "owners,tags,tableConstraints,extension"
    om_lineage_enabled: bool = False
    om_classification_map: dict[str, str] = {
        "Sensitivity.Public": "public",
        "Sensitivity.Internal": "internal",
        "Sensitivity.Confidential": "restricted",
        "Sensitivity.Restricted": "restricted",
    }
    model_metadata_sensitivities: list[str] = ["public", "internal"]
    bedrock_model_id: str = ""
    aws_region: str = "me-central-1"
    bedrock_endpoint_url: str | None = None
    llm_attempts: int = 2
    trino_host: str = "localhost"
    trino_port: int = 8080
    trino_scheme: Literal["http", "https"] = "http"
    trino_auth: Literal["demo", "jwt_passthrough", "ldap_password"] = "demo"
    trino_ca_bundle: str | None = None
    trino_token_audience: str = ""
    trino_resource_group: str = ""

    @model_validator(mode="after")
    def validate_mode(self):
        if not 1 <= self.max_rows <= 1000 or not 1 <= self.max_concurrent_jobs <= 32:
            raise ValueError("Invalid bounded execution settings")
        if self.query_timeout_seconds <= 0 or self.llm_attempts not in (1, 2):
            raise ValueError("Invalid timeout or retry settings")
        if self.auth_mode == "oidc":
            if not all((self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url)):
                raise ValueError("OIDC issuer, audience and JWKS are required")
            if not self.oidc_jwks_url.startswith("https://"):
                raise ValueError("OIDC JWKS must use TLS")
        if self.auth_mode == "trino_ldaps" and (
            self.trino_auth != "ldap_password"
            or self.trino_scheme != "https"
            or self.executor != "trino"
            or self.authorization_provider != "trino_ranger"
            or not self.ldaps_identity_namespace
        ):
            raise ValueError(
                "LDAPS requires TLS Trino password auth, Ranger authorization and identity namespace"
            )
        if self.trino_auth == "ldap_password" and self.auth_mode != "trino_ldaps":
            raise ValueError("LDAP password execution requires the verified LDAPS sign-in path")
        if self.authorization_provider == "trino_ranger" and (
            self.executor != "trino" or self.trino_scheme != "https" or self.trino_auth == "demo"
        ):
            raise ValueError("Ranger authorization probes require authenticated TLS Trino")
        if not 1 <= self.login_failure_limit <= 20 or not 10 <= self.login_failure_window_seconds <= 3600:
            raise ValueError("Invalid bounded sign-in protection")
        if self.environment == "production":
            if (
                self.auth_mode not in ("oidc", "trino_ldaps")
                or self.metadata_provider != "openmetadata"
                or self.llm_provider != "bedrock"
                or self.executor != "trino"
                or self.trino_auth not in ("jwt_passthrough", "ldap_password")
                or self.trino_scheme != "https"
                or not self.database_url.startswith("postgresql+psycopg://")
            ):
                raise ValueError(
                    "Production requires enterprise authentication, live adapters, PostgreSQL and authenticated TLS Trino"
                )
        if self.trino_auth == "demo" and self.environment == "production":
            raise ValueError("Development Trino identity is forbidden in production")
        if self.metadata_provider == "openmetadata":
            if not self.om_url.startswith("https://") or not self.om_expected_version:
                raise ValueError("Live metadata requires TLS and an explicit tested version prefix")
        if self.llm_provider == "bedrock" and not self.bedrock_model_id:
            raise ValueError("Set a supported Bedrock model / inference profile")
        return self
