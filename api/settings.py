import secrets
from pathlib import Path
from typing import Literal

from pydantic import BaseSettings, Field


class Settings(BaseSettings):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8000
    root_path: str = ""

    debug: bool = False
    reload: bool = False

    cache_ttl: int = 300

    jwt_secret: str = secrets.token_urlsafe(64)

    # Secrets for the internal service tokens, one per audience. An empty value
    # falls back to `jwt_secret`, so a deployment which has not rolled out the
    # per-audience secrets yet keeps working.
    internal_jwt_secret_auth: str = ""
    internal_jwt_secret_shop: str = ""
    internal_jwt_secret_skills: str = ""

    auth_url: str = ""
    shop_url: str = ""

    lecture_xp: int = 10

    courses: Path = Path("config/courses")

    public_base_url: str = "http://localhost:8000"
    mp4_lectures: Path = Path("lectures")
    stream_chunk_size: int = 4 * 1024 * 1024  # bytes
    stream_token_ttl: int = 8 * 60 * 60  # seconds

    internal_jwt_ttl: int = 10

    deleted_user_sweep_batch_size: int = 500
    deleted_user_sweep_rate_limit: float = 10  # auth microservice requests per second

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_tls: bool = False
    smtp_starttls: bool = True

    database_url: str = Field(
        "mysql+aiomysql://fastapi:fastapi@mariadb:3306/fastapi",
        regex=r"^(mysql\+aiomysql|postgresql\+asyncpg|sqlite\+aiosqlite)://.*$",
    )
    pool_recycle: int = 300
    pool_size: int = 20
    max_overflow: int = 20
    sql_show_statements: bool = False

    redis_url: str = Field("redis://redis:6379/1", regex=r"^redis://.*$")
    auth_redis_url: str = Field("redis://redis:6379/0", regex=r"^redis://.*$")

    sentry_dsn: str | None = None
    sentry_environment: str = "test"

    def internal_jwt_secret(self, audience: str) -> str:
        """Return the secret with which internal tokens for `audience` are signed and verified."""

        secrets_by_audience = {
            "auth": self.internal_jwt_secret_auth,
            "shop": self.internal_jwt_secret_shop,
            "skills": self.internal_jwt_secret_skills,
        }
        return secrets_by_audience.get(audience, "") or self.jwt_secret


settings = Settings()  # type: ignore
