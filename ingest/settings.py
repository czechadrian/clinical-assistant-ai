"""
settings.py — single source of truth for configuration and logging setup.

All environment reading happens here. main.py imports `settings` and
`configure_logging`; nothing else touches os.environ directly.
"""

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------

_STANDARD_LOG_KEYS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line.

    Extra fields passed via logger.info("msg", extra={"key": "val"})
    are included automatically, which makes it easy to add request-scoped
    metadata without changing the formatter.
    """

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        extra = {k: v for k, v in record.__dict__.items() if k not in _STANDARD_LOG_KEYS}
        payload: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.message,
        }
        if extra:
            payload["extra"] = extra
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(app_env: str) -> None:
    """Replace the root handler with a JSON formatter.

    Call this once, before the first log statement. Uvicorn's own loggers
    (uvicorn, uvicorn.access) are left untouched — they write to their own
    handlers. Only our application loggers (getLogger(__name__)) go through
    this formatter.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    level = logging.DEBUG if app_env == "local" else logging.INFO
    logging.basicConfig(level=level, handlers=[handler], force=True)


# ---------------------------------------------------------------------------
# Git metadata
# ---------------------------------------------------------------------------


def _get_git_commit() -> str | None:
    """Return the short HEAD commit hash, or None if git is unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            cwd=os.path.dirname(__file__) or ".",
        )
        return result.stdout.strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass
class Settings:
    supabase_url: str
    supabase_service_role_key: str
    anthropic_api_key: str
    allowed_origins: list[str]
    cors_origin_regex: str | None
    app_env: str
    git_commit: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        allowed_origins = [
            o.strip()
            for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
            if o.strip()
        ]
        cors_origin_regex = os.getenv("CORS_ORIGIN_REGEX") or None

        # Fail at import time rather than at the first request
        for origin in allowed_origins:
            if not origin.startswith(("http://", "https://")):
                raise ValueError(f"ALLOWED_ORIGINS entry missing scheme: {origin!r}")
        if cors_origin_regex:
            re.compile(cors_origin_regex)  # raises re.error on bad pattern

        return cls(
            supabase_url=os.environ["SUPABASE_URL"],
            supabase_service_role_key=os.environ["SUPABASE_SERVICE_ROLE_KEY"],
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            allowed_origins=allowed_origins,
            cors_origin_regex=cors_origin_regex,
            app_env=os.getenv("APP_ENV", "local"),
            git_commit=_get_git_commit(),
        )
