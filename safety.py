"""Erros públicos e logs sem URLs autenticadas, tokens ou tracebacks de HTTP."""

import logging
import os
import re
import sys
from collections.abc import Iterable


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[oculto]")
    text = re.sub(r"https?://api\.telegram\.org/[^\s]+", "[API Telegram]", text)
    text = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b", "[oculto]", text)
    text = re.sub(r"(?i)Bearer\s+\S+", "Bearer [oculto]", text)
    text = re.sub(r"(?i)((?:access_token|refresh_token|password|secret)=)[^\s&]+", r"\1[oculto]", text)
    return text


def clean_text(value: str, limit: int = 250) -> str:
    return " ".join(redact(value).split())[:limit]


class SafeError(Exception):
    """Só deve receber descrições constantes controladas pelo aplicativo."""

    def __init__(self, message: str, *, status: int | None = None,
                 uncertain: bool = False, retry_after: int = 0):
        super().__init__(message)
        self.status = status
        self.uncertain = uncertain
        self.retry_after = retry_after


class SecretFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()):
        super().__init__()
        self.secrets = tuple(secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), self.secrets)
        record.args = ()
        # Exceções de requests podem carregar a URL com token em seus tracebacks.
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def configure_logging(secrets: Iterable[str] = ()) -> None:
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    handler = logging.StreamHandler()
    handler.addFilter(SecretFilter(secrets))
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
