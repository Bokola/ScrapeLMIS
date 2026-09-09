"""Shared logging setup. get_logger(__name__) is called at import time by
every module in this package - configuration happens once, on first call."""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

_CONFIGURED = False
_LOG_FILE_PATH: Path | None = None


def _configure_root_logger() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    _CONFIGURED = True


def enable_file_logging(log_dir: str | Path = "./run_data/logs") -> Path:
    """Add a FileHandler capturing everything the console shows into a
    per-run timestamped file, e.g. run_data/logs/pipeline_20260826_093500.log.

    One file per run (not a single ever-growing log or a rotating one) to
    match the same per-run-timestamped convention already used for
    screenshots, downloads, and landing captures - each run's log stands
    on its own and is easy to attach for diagnosis without needing to grep
    out just the relevant lines from a long combined file.

    Safe to call more than once - only the first call actually attaches a
    handler; later calls just return the same path.
    """
    global _LOG_FILE_PATH
    _configure_root_logger()
    if _LOG_FILE_PATH is not None:
        return _LOG_FILE_PATH

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = log_dir / f"pipeline_{ts}.log"

    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logging.getLogger().addHandler(handler)
    _LOG_FILE_PATH = path
    return path


def get_logger(name: str) -> logging.Logger:
    _configure_root_logger()
    return logging.getLogger(name)
