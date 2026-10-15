"""
app/logging_setup.py
=====================
Configures logging to write to /srv/logs/geo-mcp/ (see app.config.LOG_DIR),
which must be bind-mounted from the RHEL host into the container so logs
land on the host filesystem, not inside the ephemeral container layer:

    podman run ... -v /srv/logs/geo-mcp:/srv/logs/geo-mcp:Z ...

Two log files:
  geo-mcp.log        -- standard application log (INFO+, rotated)
  geo-mcp-audit.log  -- one JSON line per geo_query call: the input prompt,
                        detected relationship, resolved entities/candidates,
                        and the SQL produced. Cheap eval data for tuning
                        thresholds/prompts later (see technical reference
                        doc, section 8 "Suggested Improvements").
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import time
from typing import Any

from app.config import AUDIT_LOG_FILE, LOG_DIR, LOG_FILE

_configured = False


def setup_logging(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Also mirror to stdout so `podman logs` still works during development.
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    _configured = True


_audit_logger: logging.Logger | None = None


def _get_audit_logger() -> logging.Logger:
    global _audit_logger
    if _audit_logger is not None:
        return _audit_logger

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("geo_mcp.audit")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.handlers.RotatingFileHandler(
        AUDIT_LOG_FILE, maxBytes=20 * 1024 * 1024, backupCount=10
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    _audit_logger = logger
    return logger


def audit(event: dict[str, Any]) -> None:
    """Write one JSON-line audit record. Never raises -- logging must not break a request."""
    try:
        record = {"ts": time.time(), **event}
        _get_audit_logger().info(json.dumps(record, default=str))
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("Failed to write audit log entry")
