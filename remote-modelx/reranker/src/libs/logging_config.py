"""Shared stdlib `logging` setup, used instead of ad-hoc `print` calls."""

from __future__ import annotations

import logging
import os
import sys

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")


def configure_logging() -> None:
    """Configure the root logger once. Logs go to stderr (stdout stays free
    for any future machine-readable output) with a timestamp, level, and
    logger name so multi-service `docker compose logs` output is
    attributable at a glance."""
    logging.basicConfig(
        level=LOG_LEVEL.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
