# src/clio/logging.py
"""Logging setup for Clio: console output plus an optional app log file.

``setup_logging`` is idempotent per process. The console handler prints to
stderr (visible in the terminal that launched clio); the file handler appends
to ``clio.log`` (override with ``CLIO_LOG_FILE`` or the ``file`` argument).
Job/ask failures are logged with full tracebacks, so a broken run always
explains itself in the terminal.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_CONFIGURED = False


def setup_logging(level: int = logging.INFO, file: str | None = None) -> None:
    """Configure the ``clio`` logger once. ``file`` enables a log file (default
    ``clio.log`` when ``True``); ``None`` means console-only."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    logger = logging.getLogger("clio")
    logger.setLevel(level)
    logger.propagate = False

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    )
    logger.addHandler(console)

    log_path = file or os.environ.get("CLIO_LOG_FILE")
    if log_path:
        try:
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                    "%Y-%m-%d %H:%M:%S",
                )
            )
            logger.addHandler(file_handler)
        except OSError as exc:
            logger.error("cannot open log file %s: %s", log_path, exc)
    logger.info("logging started (file: %s)", log_path or "none")
