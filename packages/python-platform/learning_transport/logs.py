"""json log lines."""

import json
import logging
from typing import Any

# attributes every logrecord has; anything else on a record came from `extra=`.
_STANDARD = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "service": self.service,
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        line.update({k: v for k, v in record.__dict__.items() if k not in _STANDARD})
        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


def configure(service: str) -> None:
    """log json lines at info to stderr, unless logging is already set up (for example under pytest)."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(service))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
