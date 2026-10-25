"""Structured JSON logger for Vigil MD pipeline scripts.

Sends logs to Logstash via TCP (port 5044) AND prints to console.
Falls back to console-only if Logstash is not running.

Usage:
    from scripts.logger import get_logger
    log = get_logger("fetch_sources", source="enforcement")
    log.info("file_downloaded", gcs_path="raw/enforcement/...", size_mb=45.2)
"""
import json
import logging
import socket
import time
from datetime import datetime, timezone
from pathlib import Path


LOGSTASH_HOST = "localhost"
LOGSTASH_PORT = 5044


class LogstashHandler(logging.Handler):
    """Sends structured JSON log records to Logstash via TCP."""

    def __init__(self, host: str, port: int):
        super().__init__()
        self.host = host
        self.port = port
        self._sock = None

    def _connect(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect((self.host, self.port))
            self._sock = s
        except (ConnectionRefusedError, OSError):
            self._sock = None

    def emit(self, record: logging.LogRecord):
        try:
            if self._sock is None:
                self._connect()
            if self._sock is None:
                return  # Logstash not available, skip silently

            payload = {
                "@timestamp": datetime.now(timezone.utc).isoformat(),
                "level":      record.levelname,
                "logger":     record.name,
                "message":    record.getMessage(),
                "script":     Path(record.pathname).name,
            }
            # Add any extra fields passed via log.info("msg", extra={...})
            for key, val in record.__dict__.items():
                if key not in ("message", "msg", "args", "levelname", "levelno",
                               "pathname", "filename", "module", "exc_info",
                               "exc_text", "stack_info", "lineno", "funcName",
                               "created", "msecs", "relativeCreated", "thread",
                               "threadName", "processName", "process", "name",
                               "taskName") and not key.startswith("_"):
                    payload[key] = val

            line = json.dumps(payload) + "\n"
            self._sock.sendall(line.encode("utf-8"))
        except Exception:
            self._sock = None  # reset on any error


def get_logger(name: str, **context) -> logging.Logger:
    """
    Get a logger that sends to both console and Logstash.

    Args:
        name: logger name (usually script name)
        **context: extra fields added to every log record (e.g. source="enforcement")
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured

    logger.setLevel(logging.DEBUG)

    # Console handler
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s — %(message)s"
    ))
    logger.addHandler(console)

    # Logstash handler (fails silently if Logstash not running)
    logstash = LogstashHandler(LOGSTASH_HOST, LOGSTASH_PORT)
    logger.addHandler(logstash)

    # Attach context fields to every record
    if context:
        logger = logging.LoggerAdapter(logger, context)

    return logger