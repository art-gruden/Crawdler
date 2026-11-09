"""Application settings, read from environment variables (see .env.example)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- infrastructure -------------------------------------------------
    database_url: str = "postgresql+psycopg://gcc:gcc@localhost:5432/gcc"
    redis_url: str = "redis://localhost:6379/0"
    data_dir: Path = Path("/data")
    log_level: str = "INFO"
    log_json: bool = True

    # --- authentication -------------------------------------------------
    secret_key: str = Field(default="", description="HMAC key for session tokens (>= 32 chars)")
    admin_username: str = "admin"
    admin_password: str = ""
    session_ttl_hours: int = 12
    cookie_secure: bool = True

    # --- outbound HTTP (verification, crawler) --------------------------
    crawler_user_agent: str = "GermanyCareerCrawler/0.1 (+https://github.com/your-org/germany-career-crawler)"
    # Ports a website may use. Anything else is rejected (SSRF hardening).
    net_allowed_ports: str = "80,443"
    # CIDRs that are exempt from the private-network block. Leave empty in production.
    net_allowed_private_cidrs: str = ""

    # --- OpenStreetMap Overpass -----------------------------------------
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    overpass_status_url: str = "https://overpass-api.de/api/status"
    overpass_query_timeout_s: int = 180
    overpass_maxsize_mb: int = 256
    overpass_max_response_mb: int = 64
    overpass_max_requests_per_day: int = 50
    overpass_max_mb_per_day: int = 200
    overpass_min_interval_s: int = 30
    overpass_max_retries: int = 3
    overpass_cache_hours: int = 24

    # --- website verification ------------------------------------------
    verify_concurrency: int = 8
    verify_timeout_s: float = 15.0
    verify_max_bytes: int = 512 * 1024
    verify_batch_size: int = 50
    verify_recheck_days: int = 30
    verify_due_batch: int = 200
    auto_queue_verified: bool = True

    # --- CSV import -------------------------------------------------------
    import_max_mb: int = 20
    import_max_rows: int = 50_000

    @field_validator("data_dir", mode="before")
    @classmethod
    def _path(cls, v):
        return Path(v)

    @property
    def allowed_ports(self) -> set[int]:
        return {int(p) for p in self.net_allowed_ports.split(",") if p.strip()}

    @property
    def allowed_private_cidrs(self) -> list[str]:
        return [c.strip() for c in self.net_allowed_private_cidrs.split(",") if c.strip()]

    def check_runtime_secrets(self) -> None:
        """Refuse to start the API with missing or weak credentials."""
        problems = []
        if len(self.secret_key) < 32:
            problems.append("SECRET_KEY must be at least 32 characters")
        if len(self.admin_password) < 12:
            problems.append("ADMIN_PASSWORD must be at least 12 characters")
        if problems:
            raise RuntimeError("; ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()
