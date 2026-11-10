"""Environment-backed service configuration."""

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FLUX_", env_file=".env", extra="ignore"
    )

    service_secret: SecretStr | None = None
    service_secret_file: Path | None = None
    base_url: str = "https://flux-insight.byted.org"
    iam_url: str = "https://cloud.bytedance.net/auth/api/v1/jwt"
    api_key: SecretStr | None = None
    requests_per_second: float = Field(default=8, gt=0, le=10)
    request_timeout_seconds: float = Field(default=30, gt=0, le=300)
    query_timeout_seconds: float = Field(default=120, gt=0, le=3600)
    max_pages: int = Field(default=100, ge=1, le=10000)
    max_task_ids: int = Field(default=10000, ge=1, le=1000000)
    max_concurrent_queries: int = Field(default=4, ge=1, le=100)
    max_retries: int = Field(default=3, ge=0, le=5)
    host: str = "127.0.0.1"
    port: int = Field(default=18080, ge=1, le=65535)
    options_cache_ttl_seconds: float = Field(default=60, ge=0, le=3600)
    options_cache_size: int = Field(default=128, ge=1, le=10000)
    options_pages_per_batch: int = Field(default=5, ge=1, le=100)
    options_query_timeout_seconds: float = Field(default=20, gt=0, le=300)
    fornax_base_url: str = "https://fornax.bytedance.net"
    fornax_jwt: SecretStr | None = None
    fornax_jwt_file: Path | None = None
    fornax_refresh_from_chrome: bool = False
    fornax_workspace_id: int = 7590084809894139650
    fornax_platform: str = "inner_cozeloop"
    fornax_range_days: int = Field(default=120, ge=1, le=3650)
    fornax_min_interval_seconds: float = Field(default=1.1, ge=1)
    export_dir: Path = Path("exports/jobs")
    max_export_jobs: int = Field(default=4, ge=1, le=100)
    max_tagging_jobs: int = Field(default=2, ge=1, le=16)
    export_task_timeout_seconds: float = Field(default=600, gt=0, le=7200)
    artifact_recovery_timeout_seconds: float = Field(default=60, gt=0, le=300)
    max_download_bytes: int = Field(default=256 * 1024 * 1024, ge=1)
    raw_recovery_max_sessions: int = Field(default=8, ge=1, le=100)
    raw_recovery_timeout_seconds: float = Field(default=60, gt=0, le=300)
    raw_recovery_max_bytes: int = Field(default=256 * 1024 * 1024, ge=1)
    max_export_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=1)

    @field_validator("base_url", "iam_url", "fornax_base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("URL must be an HTTP(S) URL without credentials or query")
        return value.rstrip("/")

    def read_secret(self) -> str:
        """Read only the service account credential, never a personal JWT."""
        if self.service_secret is not None:
            secret = self.service_secret.get_secret_value().strip()
        elif self.service_secret_file is not None:
            secret = self.service_secret_file.read_text(encoding="utf-8").strip()
        else:
            raise ValueError(
                "Configure FLUX_SERVICE_SECRET or FLUX_SERVICE_SECRET_FILE"
            )
        if not secret:
            raise ValueError("Service account secret is empty")
        return secret
