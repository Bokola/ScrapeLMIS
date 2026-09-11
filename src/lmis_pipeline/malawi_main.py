"""End-to-end orchestrator for Malawi's LMIS pipeline:

    login -> Reports -> View Reports -> LMIS Summary by facility ->
    [ for each of the last N months: select Program Name + Period Name ->
      xlsx -> Generate -> download ] -> stage raw -> upsert into master
      workbook, once per period.

Run with: uv run python -m lmis_pipeline.malawi_main
"""
from __future__ import annotations

import argparse
import calendar
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from .config import Config, ConfigError
from .extract import upsert_to_excel
from .landing import stage_raw_download
from .logger import enable_file_logging, get_logger
from .malawi_scraper import ScraperError, open_browser
from .schema_validation import validate_extract

log = get_logger(__name__)


class PipelineError(RuntimeError):
    pass


def recent_month_periods(n: int, today: date | None = None) -> list[str]:
    """Return the last n months in Malawi's report format (e.g. "Sep2026"),
    inclusive of the current month, oldest first - e.g. on 2026-09-10 with
    n=4: ["Jun2026", "Jul2026", "Aug2026", "Sep2026"].
    """
    today = today or date.today()
    year, month = today.year, today.month
    periods: list[str] = []
    for _ in range(n):
        periods.append(f"{calendar.month_abbr[month]}{year}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return list(reversed(periods))


def read_malawi_report(path: str | Path, period: str) -> pd.DataFrame:
    """Read a downloaded "LMIS Summary by facility" export into a clean
    DataFrame, stamped with the period it was generated for.

    CONFIRMED via a real sample export (malawi_extract.xlsx): this is a
    print-formatted report, not a plain data table. Before the real data,
    the sheet has a title row, blank rows, and a District/Reporting
    Period/Program/Report Run Date metadata row - the real column headers
    only appear several rows down, identifiable by "Line #" in the first
    column, with several blank "spacer" columns in between (leftover
    merged-cell formatting from the report's print layout) that need
    dropping rather than treated as real data columns.

    The report's own "Reporting Period:" metadata value is NOT a per-row
    field at all - it's a single report-wide value, so it can't be read
    off any individual row. Rather than parse it back out of that
    metadata row, this takes the period value already known from the
    caller's own loop (see run() below) - exactly what was used to
    generate this specific file - and stamps it onto every row as a new
    "Period" column. Without this, multiple periods' downloads upserted
    into the same master workbook would be indistinguishable from each
    other (this was a real, reported gap - period wasn't tracked in the
    master file or the landing files at all in an earlier version).
    """
    raw = pd.read_excel(path, sheet_name=0, header=None)

    header_row_idx = None
    for i, val in enumerate(raw.iloc[:, 0]):
        if str(val).strip() == "Line #":
            header_row_idx = i
            break
    if header_row_idx is None:
        raise ValueError(
            f"Could not find the 'Line #' header row in {path} - the "
            "report's layout may have changed from what this parser expects."
        )

    header_row = raw.iloc[header_row_idx]
    keep_cols = [i for i, v in enumerate(header_row) if pd.notna(v) and str(v).strip()]
    columns = [str(header_row[i]).strip() for i in keep_cols]

    data = raw.iloc[header_row_idx + 1:, keep_cols].copy()
    data.columns = columns
    data = data.dropna(how="all").reset_index(drop=True)
    data["Period"] = period

    log.info("Parsed %d rows, %d columns (incl. Period) from %s", len(data), len(data.columns), path)
    return data


def run(cfg: Config) -> Path:
    """Run the pipeline once per configured period, upserting each
    period's download into the same master workbook. Returns the path to
    the updated master workbook."""
    program = cfg.get("filters.program_name")
    if not program:
        raise PipelineError("config_malawi.yaml's filters.program_name is not set")

    n_months = cfg.get("filters.period_months", 4)
    periods = recent_month_periods(n_months)
    log.info("Will generate the report for %d period(s): %s", len(periods), periods)

    download_dir = cfg.get("output.download_dir", "./run_data_malawi/downloads")
    master_path: Path | None = None

    with open_browser(cfg) as s:
        s.ensure_logged_in()

        for period in periods:
            log.info("=== Period: %s ===", period)
            try:
                # Re-navigate to the report options page fresh for every
                # period, rather than assuming the form resets itself or
                # offers a "generate another" link after a download - not
                # confirmed either way, but re-navigating from a known-good
                # starting point is the safer default (same reasoning as
                # the original GFPVAN pipeline's per-country re-navigation).
                s.open_lmis_summary_report()
                downloaded_path = s.generate_report(
                    program=program, period=period, download_dir=download_dir
                )
            except ScraperError as e:
                log.error(
                    "Generating the report for period %s failed: %s - skipping this period",
                    period, e,
                )
                continue

            landing_path = stage_raw_download(downloaded_path, cfg)
            log.info("Staged raw download for %s to %s", period, landing_path)

            df = read_malawi_report(downloaded_path, period)

            if cfg.get("excel_columns"):
                # Only validate if a real schema/column list has been
                # filled in for Malawi - see config_malawi.yaml's own note
                # on this. Left permissive by default since there's no
                # confirmed schema yet.
                validation = validate_extract(df)
                if not validation.ok:
                    log.warning(validation.summary())
                    if cfg.get("validation.fail_on_error", False):
                        raise PipelineError(
                            f"{validation.summary()} Raw payload preserved at "
                            f"{landing_path} for diagnosis."
                        )

            master_path = upsert_to_excel(df, cfg)
            log.info("Upserted period %s into master workbook at %s", period, master_path)

    if master_path is None:
        raise PipelineError(
            "No period succeeded - master workbook was never written. "
            "See the per-period errors above and the diagnostics dumps "
            "under run_data_malawi/screenshots/."
        )

    return master_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Malawi LMIS pipeline")
    parser.add_argument("--config", default="config_malawi.yaml", type=Path)
    args = parser.parse_args()

    try:
        cfg = Config.load(args.config)
    except ConfigError as e:
        log.error("Config error: %s", e)
        return 1

    log_path = enable_file_logging(cfg.get("output.log_dir", "./run_data_malawi/logs"))
    log.info("Logging this run to %s", log_path)

    try:
        master_path = run(cfg)
    except (PipelineError, ScraperError) as e:
        log.error("Pipeline failed: %s", e)
        return 1

    print(f"Master workbook: {master_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
