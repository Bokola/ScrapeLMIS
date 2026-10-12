"""End to end orchestrator for Nigeria's LMIS forward load (family planning):

    work out the latest completed two month window -> login -> wait for the
    data sync -> open the Analytics page -> set that window with Prev and Next ->
    click LMIS & Service Data Export -> check it is an
    xlsx whose first sheet is the expected one -> land it untouched as
    LMIS_NG_<year>_<m1>_to_<m2>.xlsx -> write its family planning sheet to the
    extracts folder under the same file name

The data is never read into pandas or edited, the landed file is a byte for
byte copy of what the site sent.

Run with: uv run python -m lmis_pipeline.nigeria_main
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd

from .config import Config, ConfigError
from .logger import enable_file_logging, get_logger
from .nigeria_scraper import open_browser, window_index
from .periods import bimonthly_window_label, latest_completed_bimonthly_window
from .scraper_base import ScraperError

log = get_logger(__name__)

SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


class PipelineError(RuntimeError):
    pass


def landing_file_name(cfg: Config, year: int, first_month: int, second_month: int) -> str:
    """Fill landing.file_template, e.g. LMIS_NG_2026_07_to_08.xlsx."""
    template = cfg.get("landing.file_template")
    if not template:
        raise ConfigError("landing.file_template is not set in the config")
    return template.format(year=year, m1=f"{first_month:02d}", m2=f"{second_month:02d}")


def first_sheet_name(path: Path) -> str:
    """Name of the first sheet in tab order, read from the workbook's own
    manifest so no cell data is parsed or typed."""
    try:
        with zipfile.ZipFile(path) as archive:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        raise PipelineError(f"{path.name} is not a readable xlsx workbook: {e}") from e

    sheets = workbook.find(f"{SHEET_NS}sheets")
    first = sheets.find(f"{SHEET_NS}sheet") if sheets is not None else None
    name = first.get("name") if first is not None else None
    if not name:
        raise PipelineError(f"{path.name} has no sheets")
    return name


def stage_landing(downloaded_path: Path, file_name: str, cfg: Config) -> Path:
    """Copy the download into the landing folder under its final name. A
    rerun for the same window replaces the file. Written to a temporary
    name first so a crash never leaves a half copied file behind."""
    landing_dir = Path(cfg.get("landing.dir", "./run_data_nigeria/landing"))
    landing_dir.mkdir(parents=True, exist_ok=True)
    dest = landing_dir / file_name
    partial = dest.with_name(dest.name + ".part")
    shutil.copy2(downloaded_path, partial)
    partial.replace(dest)
    return dest


def window_parts(index: int) -> tuple[int, int, int]:
    """(year, first month, second month) of a window position, the inverse of window_index."""
    year, k = divmod(index, 6)
    return year, 2 * k + 1, 2 * k + 2


def write_extract(landing_path: Path, file_name: str, cfg: Config) -> Path:
    """Write the family planning sheet of a landed file to the extracts folder
    under the same file name as the landing file. Cells keep the type they have
    in the workbook, so a code stored as text keeps its leading zero and a
    number stays a number. A rerun replaces the file."""
    sheet = cfg.get("export.expected_first_sheet")
    try:
        df = pd.read_excel(landing_path, sheet_name=sheet, dtype=object)
    except (ValueError, KeyError) as e:
        raise PipelineError(f"could not read sheet {sheet!r} from {landing_path.name}: {e}") from e
    df = df.dropna(how="all")
    if df.empty:
        raise PipelineError(f"sheet {sheet!r} in {landing_path.name} has no rows")
    out_dir = Path(cfg.get("output.dir", "./run_data_nigeria/extracts"))
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / file_name
    partial = dest.with_name(dest.name + ".part")
    df.to_excel(partial, sheet_name=sheet, index=False, engine="openpyxl")
    partial.replace(dest)
    log.info("Wrote the %s sheet (%d rows) to %s", sheet, len(df), dest)
    return dest


def landed_windows(cfg: Config) -> list[Path]:
    """Every file in the landing folder named like the landing template, oldest first."""
    template = cfg.get("landing.file_template")
    pattern = re.compile(
        re.escape(template)
        .replace(r"\{year\}", r"(?P<year>\d{4})")
        .replace(r"\{m1\}", r"(?P<m1>\d{2})")
        .replace(r"\{m2\}", r"(?P<m2>\d{2})")
    )
    found = []
    for path in Path(cfg.get("landing.dir")).glob("*"):
        m = pattern.fullmatch(path.name)
        if m:
            found.append((window_index(bimonthly_window_label(int(m["year"]), int(m["m1"]))), path))
    return [path for _, path in sorted(found)]


def rebuild_extract(cfg: Config) -> list[Path]:
    """Write the extract of every landed file, without opening a browser."""
    landed = landed_windows(cfg)
    if not landed:
        raise PipelineError(f"no landed files found in {cfg.get('landing.dir')}")
    return [write_extract(path, path.name, cfg) for path in landed]


def _require_checks(cfg: Config) -> tuple[str, str]:
    sheet = cfg.get("export.expected_first_sheet")
    if not sheet:
        raise ConfigError("export.expected_first_sheet is not set in the config")
    extension = cfg.get("export.expected_extension")
    if not extension:
        raise ConfigError("export.expected_extension is not set in the config")
    return sheet, extension


def windows_to_load(cfg: Config, today: date | None, historical: bool) -> list[int]:
    """Window positions to load, newest first, which is the fewest Prev presses
    since the page opens on the latest window."""
    year, first_month, _ = latest_completed_bimonthly_window(today)
    latest = window_index(bimonthly_window_label(year, first_month))
    if not historical:
        return [latest]
    try:
        start_label = cfg.get("historical.start")
        if not start_label:
            raise ConfigError("historical.start is not set in the config")
        start = window_index(start_label)
        end_setting = cfg.get("historical.end", "auto")
        end = latest if end_setting == "auto" else window_index(end_setting)
    except ScraperError as e:
        raise ConfigError(f"historical window in the config: {e}") from e
    if end > latest:
        raise ConfigError(
            f"historical.end {end_setting!r} is not a completed window yet, the latest is "
            f"{bimonthly_window_label(year, first_month)}"
        )
    if start > end:
        raise ConfigError(f"historical.start {start_label!r} is after the end of the range")
    return list(range(end, start - 1, -1))


def fetch_window(s, index: int, cfg: Config) -> tuple[Path, Path]:
    """Select one window in an open session, download it, check it, land it
    untouched and write its extract. Returns (landing path, extract path)."""
    expected_sheet, expected_extension = _require_checks(cfg)
    year, first_month, second_month = window_parts(index)
    label = bimonthly_window_label(year, first_month)
    file_name = landing_file_name(cfg, year, first_month, second_month)
    log.info("Window %s, landing file: %s", label, file_name)

    s.select_window(label)
    downloaded_path = s.download_export(cfg.get("output.download_dir", "./run_data_nigeria/downloads"))

    # checked on the copy in the downloads folder, which is never
    # overwritten, so a wrong export can not replace a good landed file
    if downloaded_path.suffix.lower() != expected_extension.lower():
        raise PipelineError(
            f"the site sent {downloaded_path.name}, not a {expected_extension} file, so it is "
            f"probably a different export than LMIS & Service Data Export. The raw "
            f"download is kept at {downloaded_path} and nothing was landed."
        )
    actual_sheet = first_sheet_name(downloaded_path)
    if actual_sheet != expected_sheet:
        raise PipelineError(
            f"first sheet is {actual_sheet!r}, expected {expected_sheet!r}. The raw "
            f"download is kept at {downloaded_path} and nothing was landed."
        )

    landing_path = stage_landing(downloaded_path, file_name, cfg)
    log.info("Landed %s", landing_path)
    return landing_path, write_extract(landing_path, file_name, cfg)


def run(cfg: Config, today: date | None = None) -> Path:
    """Run the forward load once: the latest completed two month window.
    Returns the landed file's path, the extract has the same file name in the
    extracts folder."""
    _require_checks(cfg)
    (index,) = windows_to_load(cfg, today, historical=False)
    with open_browser(cfg) as s:
        s.login()
        s.wait_for_sync()
        s.open_export_page()
        landing_path, _ = fetch_window(s, index, cfg)
    return landing_path


def run_historical(cfg: Config, today: date | None = None) -> tuple[list[Path], list[str], list[str]]:
    """Load every window from historical.start to historical.end in one
    browser session, newest first. A window that fails is logged and the next
    one is tried, up to export.max_consecutive_failures in a row. Returns
    (landed paths, labels that failed, labels not attempted)."""
    _require_checks(cfg)
    indices = windows_to_load(cfg, today, historical=True)
    landing_dir = Path(cfg.get("landing.dir", "./run_data_nigeria/landing"))
    extract_dir = Path(cfg.get("output.dir", "./run_data_nigeria/extracts"))
    todo = []
    for index in indices:
        year, first_month, second_month = window_parts(index)
        name = landing_file_name(cfg, year, first_month, second_month)
        if cfg.get("historical.skip_existing", True) and (landing_dir / name).exists() and (extract_dir / name).exists():
            log.info("Skipping %s, %s is already landed and extracted", bimonthly_window_label(year, first_month), name)
        else:
            todo.append(index)
    log.info(
        "Historical load of %d window(s), %s to %s, %d to fetch",
        len(indices), bimonthly_window_label(*window_parts(indices[-1])[:2]),
        bimonthly_window_label(*window_parts(indices[0])[:2]), len(todo),
    )
    if not todo:
        return [], [], []

    max_failures = cfg.get("export.max_consecutive_failures", 2)
    loaded: list[Path] = []
    failed: list[str] = []
    not_attempted: list[str] = []
    streak = 0
    with open_browser(cfg) as s:
        s.login()
        s.wait_for_sync()
        s.open_export_page()
        for position, index in enumerate(todo):
            label = bimonthly_window_label(*window_parts(index)[:2])
            try:
                landing_path, _ = fetch_window(s, index, cfg)
            except (PipelineError, ScraperError) as e:
                log.error("Window %s failed: %s", label, e)
                failed.append(label)
                streak += 1
                if streak >= max_failures:
                    not_attempted = [bimonthly_window_label(*window_parts(i)[:2]) for i in todo[position + 1:]]
                    log.error("Stopping, %d windows failed in a row", streak)
                    break
            else:
                loaded.append(landing_path)
                streak = 0
    return loaded, failed, not_attempted


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the Nigeria LMIS forward load for family planning"
    )
    parser.add_argument("--config", default="config_nigeria.yaml", type=Path)
    parser.add_argument(
        "--extract-only", action="store_true",
        help="no browser, write the family planning extract of every landed file",
    )
    parser.add_argument(
        "--historical", action="store_true",
        help="load every window from historical.start to historical.end instead of the latest one "
        "(also on when historical.enabled is true in the config)",
    )
    args = parser.parse_args()

    try:
        cfg = Config.load(args.config)
    except ConfigError as e:
        log.error("Config error: %s", e)
        return 1

    log_path = enable_file_logging(cfg.get("output.log_dir", "./run_data_nigeria/logs"))
    log.info("Logging this run to %s", log_path)

    try:
        if args.extract_only:
            paths = rebuild_extract(cfg)
            print(f"Extracted {len(paths)} file(s) to {paths[0].parent}")
            return 0
        if args.historical or cfg.get("historical.enabled", False):
            loaded, failed, not_attempted = run_historical(cfg)
            print(f"Landed and extracted {len(loaded)} window(s)")
            if failed or not_attempted:
                print(f"Failed: {', '.join(failed) or 'none'}")
                print(f"Not attempted: {', '.join(not_attempted) or 'none'}")
                print("Run it again, windows that are done are skipped")
                return 1
            return 0
        landing_path = run(cfg)
    except (PipelineError, ScraperError, ConfigError) as e:
        log.error("Pipeline failed: %s", e)
        return 1

    print(f"Landed: {landing_path}")
    print(f"Extract: {Path(cfg.get('output.dir', './run_data_nigeria/extracts')) / landing_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
