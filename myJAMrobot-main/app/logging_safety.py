from __future__ import annotations

import logging
import os
import re
from typing import Any, Iterable, Mapping

_AUTHORIZATION_RE = re.compile(
    r"(?i)\b(authorization)[\"']?\s*[:=]\s*[\"']?(?:(?:Bearer|Basic)\s+)?([^&\s,\"'}]+)"
)
_SECRET_QUERY_RE = re.compile(
    r"(?i)\b(api_key|api_sig|sk|access_token|refresh_token|client_secret|bot_token|token|hash|initData)"
    r"[\"']?\s*[:=]\s*[\"']?([^&\s,\"'}]+)"
)
_BEARER_RE = re.compile(r"(?i)\b(Bearer)\s+([A-Za-z0-9._~+/=-]{12,})")
_TELEGRAM_BOT_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
_CONFIGURED = False
_FACTORY_INSTALLED = False
_ORIGINAL_RECORD_FACTORY = logging.getLogRecordFactory()


def _known_secret_values() -> Iterable[str]:
    names = (
        "TELEGRAM_BOT_TOKEN",
        "MYJAM_TELEGRAM_BOT_TOKEN",
        "TR3_TELEGRAM_BOT_TOKEN",
        "BOT_TOKEN",
        "LASTFM_API_KEY",
        "MYJAM_LASTFM_API_KEY",
        "TR3_LASTFM_API_KEY",
        "LASTFM_API_SECRET",
        "MYJAM_LASTFM_API_SECRET",
        "TR3_LASTFM_API_SECRET",
        "LASTFM_SESSION_KEY",
        "MYJAM_LASTFM_SESSION_KEY",
        "TR3_LASTFM_SESSION_KEY",
        "LASTFM_SK",
        "TR3_LASTFM_SK",
        "SPOTIFY_CLIENT_SECRET",
        "MYJAM_SPOTIFY_CLIENT_SECRET",
        "TR3_SPOTIFY_CLIENT_SECRET",
        "SPOTIFY_CLIENT_ID",
        "MYJAM_SPOTIFY_CLIENT_ID",
        "TR3_SPOTIFY_CLIENT_ID",
        "WEBAPP_SECRET",
        "MYJAM_WEBAPP_SECRET",
        "TR3_WEBAPP_SECRET",
        "TELEGRAM_WEBHOOK_SECRET",
        "MYJAM_TELEGRAM_WEBHOOK_SECRET",
        "TR3_TELEGRAM_WEBHOOK_SECRET",
    )
    seen: set[str] = set()
    for name in names:
        value = os.getenv(name)
        if value and len(value) >= 8 and value not in seen:
            seen.add(value)
            yield value


def redact_secrets(text: object) -> str:
    value = str(text)
    value = _AUTHORIZATION_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", value)
    value = _SECRET_QUERY_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", value)
    value = _BEARER_RE.sub(lambda m: f"{m.group(1)} [REDACTED]", value)
    value = _TELEGRAM_BOT_TOKEN_RE.sub("[REDACTED_TELEGRAM_BOT_TOKEN]", value)
    for secret in _known_secret_values():
        value = value.replace(secret, "[REDACTED_SECRET]")
    return value


def _redact_arg(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, bytes):
        try:
            return redact_secrets(value.decode("utf-8", errors="replace"))
        except Exception:
            return value
    return value


def _redact_args(args: Any) -> Any:
    """Redact logging args without changing their container shape.

    Uvicorn's access formatter expects record.args to contain exactly five
    elements. Earlier hardening replaced args with an empty tuple after calling
    getMessage(), which broke every access log with:
    ValueError: not enough values to unpack (expected 5, got 0).
    """
    if isinstance(args, tuple):
        return tuple(_redact_arg(item) for item in args)
    if isinstance(args, Mapping):
        return {key: _redact_arg(value) for key, value in args.items()}
    return _redact_arg(args)


def _redacting_record_factory(*args, **kwargs) -> logging.LogRecord:
    record = _ORIGINAL_RECORD_FACTORY(*args, **kwargs)
    try:
        record.msg = redact_secrets(record.msg)
        record.args = _redact_args(record.args)
        if record.exc_info:
            formatter = logging.Formatter()
            record.exc_text = redact_secrets(formatter.formatException(record.exc_info))
            record.exc_info = None
        if record.stack_info:
            record.stack_info = redact_secrets(record.stack_info)
    except Exception:
        record.msg = "[LOG_REDACTION_FAILED]"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
    return record


class SecretRedactionFormatter(logging.Formatter):
    def __init__(self, wrapped: logging.Formatter | None = None) -> None:
        super().__init__()
        self._wrapped = wrapped or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(self._wrapped.format(record))


def _wrap_handler_formatter(handler: logging.Handler) -> None:
    if isinstance(handler.formatter, SecretRedactionFormatter):
        return
    handler.setFormatter(SecretRedactionFormatter(handler.formatter))


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_secrets(record.msg)
            record.args = _redact_args(record.args)
        except Exception:
            record.msg = "[LOG_REDACTION_FAILED]"
            record.args = ()
        return True


def configure_safe_logging() -> None:
    global _CONFIGURED, _FACTORY_INSTALLED
    if not _FACTORY_INSTALLED:
        logging.setLogRecordFactory(_redacting_record_factory)
        _FACTORY_INSTALLED = True

    redaction_filter = SecretRedactionFilter()
    root = logging.getLogger()
    if not any(isinstance(item, SecretRedactionFilter) for item in root.filters):
        root.addFilter(redaction_filter)
    for handler in root.handlers:
        if not any(isinstance(item, SecretRedactionFilter) for item in handler.filters):
            handler.addFilter(redaction_filter)
        _wrap_handler_formatter(handler)
    _CONFIGURED = True

    # httpx/httpcore INFO logs include full URLs, which may contain query secrets.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
