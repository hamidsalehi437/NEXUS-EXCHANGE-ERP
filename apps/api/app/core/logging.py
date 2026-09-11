"""Structured logging with correlation IDs and secret/PII redaction.

Every log record carries the request correlation id (``request_id``) so an audit
row, an API response and an nginx access line can be joined. Secrets and
credential material are redacted by key **and** by pattern before a record is
rendered (SECURITY.md §4): passwords, tokens, hashes, API keys and authorization
headers never reach the log stream.
"""

from __future__ import annotations

import logging
import re
import sys
from contextvars import ContextVar
from typing import Any

import structlog

REQUEST_ID_HEADER = "X-Request-Id"

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Keys whose value must never be logged, matched case-insensitively on substrings.
REDACTED_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "private_key",
    "credential",
    "cookie",
    "hash",
)
REDACTION_PLACEHOLDER = "[redacted]"

# Patterns applied to string values (e.g. a DSN embedded in a message).
_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # password inside a DSN: scheme://user:***@host
    (
        re.compile(r"(?P<scheme>[a-z0-9+]+://[^:/@\s]+:)(?P<secret>[^@\s]+)(?=@)"),
        r"\g<scheme>[redacted]",
    ),
    # Bearer tokens
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"), "Bearer [redacted]"),
    # Argon2 / bcrypt style hashes
    (re.compile(r"\$(argon2[a-z0-9]*|2[aby])\$[^\s\"']+"), REDACTION_PLACEHOLDER),
)


def _should_redact_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in REDACTED_KEY_PARTS)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        redacted = value
        for pattern, replacement in _REDACTION_PATTERNS:
            redacted = pattern.sub(replacement, redacted)
        return redacted
    return value


def redact_processor(_logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: drop or mask anything secret-looking."""
    for key in list(event_dict.keys()):
        if _should_redact_key(key):
            event_dict[key] = REDACTION_PLACEHOLDER
        else:
            event_dict[key] = _redact_value(event_dict[key])
    return event_dict


def add_request_context(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Attach the current request id to every record."""
    request_id = request_id_var.get()
    if request_id is not None:
        event_dict.setdefault("request_id", request_id)
    return event_dict


def configure_logging(*, level: str = "INFO", fmt: str = "json") -> None:
    """Configure structlog for the process (API or worker)."""
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        add_request_context,
        redact_processor,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, sqlalchemy, celery) through structlog's level.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.getLevelNamesMapping()[level],
        force=True,
    )
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).handlers = []


def get_logger(name: str) -> Any:
    """Return a bound structlog logger."""
    return structlog.get_logger(name)
