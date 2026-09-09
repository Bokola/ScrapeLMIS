"""Landing zone: preserve an untouched copy of whatever SIMAM's "Download
results" actually handed us, before any schema validation or reshaping
touches it.

Why this exists as its own step: if schema validation later finds a
problem (e.g. SIMAM renamed a column, or the report's default scope
changed), you can diagnose against exactly what was downloaded without
re-running the browser. Nothing here is deleted or overwritten - every
run's raw file gets its own timestamped copy.
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from .config import Config
from .logger import get_logger

log = get_logger(__name__)


def stage_raw_download(downloaded_path: str | Path, cfg: Config, run_id: str | None = None) -> Path:
    """Copy the raw downloaded file (as saved by
    scraper.download_results_xlsx) into the landing zone untouched. Returns
    the landing-zone path.
    """
    downloaded_path = Path(downloaded_path)
    landing_dir = Path(cfg.get("landing.dir", "./run_data/landing"))
    landing_dir.mkdir(parents=True, exist_ok=True)

    run_id = run_id or datetime.now().strftime("%Y%m%dT%H%M%S")
    dest = landing_dir / f"lmis_mz_raw_{run_id}{downloaded_path.suffix}"
    shutil.copy2(downloaded_path, dest)

    log.info("Staged raw download to %s", dest)
    return dest
