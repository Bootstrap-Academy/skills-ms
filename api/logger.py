import logging
import re
import sys
from typing import Any

import sentry_sdk
from fastapi import FastAPI
from sentry_sdk.integrations.aiohttp import AioHttpIntegration
from sentry_sdk.integrations.logging import LoggingIntegration, ignore_logger
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from uvicorn.config import LOGGING_CONFIG
from uvicorn.logging import DefaultFormatter

from .settings import settings


def redact_asset_grants(value: Any) -> Any:
    """Remove private capability values from logs, errors and Sentry payloads."""
    if isinstance(value, str):
        return re.sub(r"(lesson-assets/)[^/\s?#]+", r"\1[redacted]", value)
    if isinstance(value, dict):
        private_frame = value.get("module") in (
            "api.services.private_lesson_modules",
            "api.endpoints.lesson_assets",
        ) or str(value.get("filename", "")).endswith(
            ("api/services/private_lesson_modules.py", "api/endpoints/lesson_assets.py")
        )
        return {
            key: (
                "[redacted]"
                if key in ("grant", "token") or (private_frame and key == "vars")
                else redact_asset_grants(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(redact_asset_grants(item) for item in value)
    return value


class AssetGrantFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_asset_grants(record.msg)
        record.args = redact_asset_grants(record.args)
        return True


def redact_sentry_event(event: Any, hint: Any) -> Any:
    return redact_asset_grants(event)


def setup_sentry(app: FastAPI, dsn: str, name: str, version: str) -> None:
    """Initialize sentry connection."""

    sentry_sdk.init(
        dsn=dsn,
        attach_stacktrace=True,
        shutdown_timeout=5,
        integrations=[
            AioHttpIntegration(),
            SqlalchemyIntegration(),
            LoggingIntegration(level=logging.DEBUG, event_level=logging.WARNING),
        ],
        release=f"{name}@{version}",
        environment=settings.sentry_environment,
        before_send=redact_sentry_event,
        before_send_transaction=redact_sentry_event,
    )
    ignore_logger("uvicorn.error")


logging_formatter = DefaultFormatter(fmt := "[%(asctime)s] %(levelprefix)s %(message)s")
LOGGING_CONFIG["formatters"]["default"]["fmt"] = fmt
LOGGING_CONFIG["formatters"]["access"][
    "fmt"
] = '[%(asctime)s] %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'
LOGGING_CONFIG.setdefault("filters", {})["private_lesson_grants"] = {"()": "api.logger.AssetGrantFilter"}
for handler in LOGGING_CONFIG["handlers"].values():
    handler.setdefault("filters", []).append("private_lesson_grants")

logging_handler = logging.StreamHandler(sys.stdout)
logging_handler.setFormatter(logging_formatter)
logging_handler.addFilter(AssetGrantFilter())


def get_logger(name: str) -> logging.Logger:
    """Get a logger with a given name."""

    logger: logging.Logger = logging.getLogger(name)
    logger.addHandler(logging_handler)
    logger.setLevel(settings.log_level)

    return logger
