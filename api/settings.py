import secrets
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="forbid", coerce_numbers_to_str=True)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8000
    root_path: str = ""

    debug: bool = False
    reload: bool = False

    cache_ttl: int = 300
    # Prepare the readers without activating the durable backend privacy policy.
    profile_publications_enabled: bool = False

    jwt_secret: str = secrets.token_urlsafe(64)

    # Secrets for the internal service tokens, one per audience. An empty value
    # falls back to `jwt_secret`, so a deployment which has not rolled out the
    # per-audience secrets yet keeps working.
    internal_jwt_secret_auth: str = ""
    internal_jwt_secret_shop: str = ""
    internal_jwt_secret_skills: str = ""
    internal_jwt_secret_challenges: str = ""

    auth_url: str = ""
    shop_url: str = ""
    # Operator-owned service origin; room content and client requests supply only IDs.
    challenges_url: str = Field(default="http://127.0.0.1:8005", pattern=r"^https?://[^?#@]+$")
    rooms_enabled: bool = False
    # Disabled until the backend policy API is deployed. Never activates new terms.
    daily_limit_policy_enabled: bool = False
    learning_rooms_content: Path | None = None
    learning_rooms_exercise_refs: dict[str, dict[str, str]] = Field(default_factory=dict)
    lesson_module_origins: list[str] = Field(default_factory=list)
    lesson_module_local_development: bool = False
    private_lesson_modules_root: Path | None = None
    private_lesson_module_grant_ttl: int = Field(default=3600, ge=60, le=8 * 60 * 60)
    # LLM gateway (llm-ms). The grant key is shared only with llm-ms and signs lesson grants. Grading
    # verdicts from llm-ms are checked with their own verdict key; there is no fallback to the grant key.
    # Like in llm-ms, each key is at least 32 bytes and differs from every other key (also JWT_SECRET and
    # INTERNAL_JWT_SECRET_*). Give each key either as a value or as a credential file (`*_FILE`, trailing
    # CR/LF removed as llm-ms does), never both. Without a usable key, grants or graded completions are off.
    llm_grant_secret: str = ""
    llm_grant_secret_file: Path | None = None
    llm_verdict_secret: str = ""
    llm_verdict_secret_file: Path | None = None
    # The environment whose verdicts count here (llm-ms `grading.environment`, claim `env`), e.g. "prod" on
    # the production host and "test" on the test host; 1 to 32 characters a-z, 0-9 and -, starting with a
    # letter. Empty or invalid: graded completions are off, like without a verdict key.
    llm_verdict_env: str = ""
    llm_grant_ttl: int = Field(default=2 * 60 * 60, ge=60, le=8 * 60 * 60)
    character_areas: Path = Path(__file__).parent / "content/character_areas.json"

    lecture_xp: int = 10

    courses: Path = Path("config/courses")
    private_courses_directory: Path | None = None

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
        default="mysql+aiomysql://fastapi:fastapi@mariadb:3306/fastapi",
        pattern=r"^(mysql\+aiomysql|postgresql\+asyncpg|sqlite\+aiosqlite)://.*$",
    )
    pool_recycle: int = 300
    pool_size: int = 20
    max_overflow: int = 20
    sql_show_statements: bool = False

    redis_url: str = Field(default="redis://redis:6379/1", pattern=r"^redis://.*$")
    auth_redis_url: str = Field(default="redis://redis:6379/0", pattern=r"^redis://.*$")

    sentry_dsn: str | None = None
    sentry_environment: str = "test"

    def internal_jwt_secret(self, audience: str) -> str:
        """Return the secret with which internal tokens for `audience` are signed and verified."""

        secrets_by_audience = {
            "auth": self.internal_jwt_secret_auth,
            "shop": self.internal_jwt_secret_shop,
            "skills": self.internal_jwt_secret_skills,
            "challenges": self.internal_jwt_secret_challenges,
        }
        return secrets_by_audience.get(audience, "") or self.jwt_secret

    @field_validator("llm_grant_secret_file", "llm_verdict_secret_file", mode="before")
    @classmethod
    def unset_empty_path(cls, value: Any) -> Any:
        # An empty `*_FILE=` in an environment file means "not configured", not the working directory.
        return None if value == "" else value


settings = Settings()
