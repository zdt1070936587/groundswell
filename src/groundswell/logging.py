from __future__ import annotations

import contextlib
import os
import sys

from loguru import logger

_configured = False


def setup_logging(level: str | None = None) -> None:
    global _configured
    if _configured:
        return
    for stream in (sys.stdout, sys.stderr):
        # Windows 管道下默认 GBK，避免因个别字符崩溃
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="replace")
    logger.remove()
    logger.add(
        sys.stderr,
        level=level or os.getenv("GS_LOG_LEVEL", "INFO"),
        format="<green>{time:HH:mm:ss}</green> <level>{level: <7}</level> {message}",
    )
    _configured = True
