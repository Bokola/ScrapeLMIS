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
from .extract import upsert_to_excel_and_csv
from .logger import enable_file_logging, get_logger
from .malawi_scraper import ScraperError, open_browser
from .periods import month_abbr_period, month_window
from .product_matching import filter_to_known_products, load_product_category_list, match_product_category
from .schema_validation import MALAWI_SUMMARY_SCHEMA, validate_extract

log = get_logger(__name__)


class PipelineError(RuntimeError):
    pass


def recent_month_periods(n: int, offset: int = 0, today: date | None = None) -> list[str]:
    """Return n months in Malawi's report format (e.g. "Sep2026"), oldest
    first, ending offset months before the current month - e.g. on
    2026-09-10 with n=4, offset=0 (the original default): ["Jun2026",
    "Jul2026", "Aug2026", "Sep2026"]. With n=2, offset=2: ["Jun2026",
    "Jul2026"] - skipping August and September entirely.

    Thin wrapper over periods.month_window() (shared with Mozambique's
    equivalent client-side filter in main.py) plus Malawi's own
    "Mon"+"YYYY" formatting.
    """
    return [month_abbr_period(y, m) for y, m in month_window(n, offset, today)]


_MONTH_ABBR_TO_NUM = {abbr: i for i, abbr in enumerate(calendar.month_abbr) if abbr}


def _format_period_date(period: str) -> str:
    """Convert a period string in the exact format the scraper needs to
    select in the site's own dropdown (e.g. "Jun2026") into "01/06/2026"
    for the master workbook's own Period column, per explicit request.
    Day is always "01" - these are monthly periods, there's no real day
    component. Locale-independent: maps the abbreviation back to a month
    number using the same calendar.month_abbr table recent_month_periods()
    used to generate it in the first place, rather than a locale-dependent
    strptime.
    """
    month_abbr, year = period[:3], period[3:]
    return f"01/{_MONTH_ABBR_TO_NUM[month_abbr]:02d}/{year}"


def stage_malawi_landing_csv(downloaded_path: str | Path, period_date: str, cfg: Config) -> Path:
    """Stage the raw downloaded report as a CSV in the landing zone, named
    LMIS_MW_<period> (period_date's slashes replaced with dashes, since "/"
    isn't valid in a filename) - e.g. period_date "01/06/2026" ->
    LMIS_MW_01-06-2026.csv, per explicit request.

    One file per period, not a timestamp: re-running the same period
    overwrites its own landing file rather than accumulating duplicate
    timestamped copies, which is the natural reading of a period-named
    (not timestamp-named) file.

    Dumps the ENTIRE raw sheet as-is (title rows, metadata rows,
    everything) translated to CSV, not the cleaned data table - matching
    this project's existing landing-zone convention elsewhere of
    preserving an untouched capture before any parsing/reshaping happens
    (see read_malawi_report() for where the actual cleanup happens
    instead).
    """
    landing_dir = Path(cfg.get("landing.dir", "./run_data_malawi/landing"))
    landing_dir.mkdir(parents=True, exist_ok=True)

    raw = pd.read_excel(downloaded_path, sheet_name=0, header=None)
    filename_period = period_date.replace("/", "-")
    dest = landing_dir / f"LMIS_MW_{filename_period}.csv"
    raw.to_csv(dest, index=False, header=False)

    log.info("Staged raw download (as CSV) to %s", dest)
    return dest


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
    "Period" column, reformatted to "01/mm/yyyy" per explicit request
    (the raw "Jun2026"-style string is what the scraper needs for its own
    dropdown selection, not what should end up in the output data).

    "Line #" (the print layout's own row-pagination number, not real data)
    is dropped and replaced by this Period column as the first column,
    per explicit request.
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

    if "Line #" in data.columns:
        data = data.drop(columns=["Line #"])
    data.insert(0, "Period", _format_period_date(period))

    log.info("Parsed %d rows, %d columns (incl. Period) from %s", len(data), len(data.columns), path)
    return data


def filter_products(df: pd.DataFrame, products: list[str]) -> pd.DataFrame:
    """Restrict df to the given exact "Product" values, since Malawi's
    report form has no per-product filter of its own (unlike Mozambique's
    "Nome do produto" widget) - see config_malawi.yaml's filters.products.
    A no-op if products is empty, matching Mozambique's own "empty means
    pull everything" convention.
    """
    if not products:
        return df
    before = len(df)
    filtered = df[df["Product"].isin(products)].reset_index(drop=True)
    log.info(
        "Filtered to %d configured product(s): %d row(s) -> %d row(s) kept",
        len(products), before, len(filtered),
    )
    missing = set(products) - set(df["Product"].unique())
    if missing:
        log.warning(
            "%d configured product(s) matched nothing in this download - "
            "check for an exact-text mismatch (spacing/punctuation): %s",
            len(missing), sorted(missing),
        )
    return filtered


def run(cfg: Config) -> tuple[Path, Path]:
    """Run the pipeline once per configured period, upserting each
    period's download into the same master files. Returns (xlsx_path,
    csv_path) - both are always written together, see
    extract.upsert_to_excel_and_csv()."""
    program = cfg.get("filters.program_name")
    if not program:
        raise PipelineError("config_malawi.yaml's filters.program_name is not set")

    n_months = cfg.get("filters.period_months", 4)
    offset_months = cfg.get("filters.period_offset_months", 0)
    periods = recent_month_periods(n_months, offset=offset_months)
    log.info("Will generate the report for %d period(s): %s", len(periods), periods)

    download_dir = cfg.get("output.download_dir", "./run_data_malawi/downloads")
    result: tuple[Path, Path] | None = None

    product_category_list = None
    category_file = cfg.get("filters.product_category_file")
    if category_file:
        product_category_list = load_product_category_list(category_file)
        log.info(
            "Loaded %d product/category mapping(s) from %s",
            len(product_category_list), category_file,
        )

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

            period_date = _format_period_date(period)
            landing_path = stage_malawi_landing_csv(downloaded_path, period_date, cfg)
            log.info("Staged raw download for %s to %s", period, landing_path)

            df = read_malawi_report(downloaded_path, period)
            df = filter_products(df, cfg.get("filters.products", []))

            if product_category_list:
                # Master-list whitelist + Category, per explicit request -
                # same mechanism as Mozambique's HIV extraction (see
                # product_matching.py), reused here with Malawi's own
                # "Product" column name. NOT yet validated against real
                # Malawi HIV product naming - the matching approach was
                # tuned against real Mozambique data only so far.
                df = filter_to_known_products(df, product_category_list, product_column="Product")
                df["Category"] = df["Product"].map(
                    lambda name: (match_product_category(name, product_category_list) or (name, ""))[1]
                )

            if cfg.get("excel_columns"):
                # Only validate if a real schema/column list has been
                # filled in for Malawi - see config_malawi.yaml's own note
                # on this. Left permissive by default since there's no
                # confirmed schema yet.
                validation = validate_extract(df, schema=MALAWI_SUMMARY_SCHEMA)
                if not validation.ok:
                    log.warning(validation.summary())
                    if cfg.get("validation.fail_on_error", False):
                        raise PipelineError(
                            f"{validation.summary()} Raw payload preserved at "
                            f"{landing_path} for diagnosis."
                        )

            result = upsert_to_excel_and_csv(df, cfg, text_format_columns=["Period"])
            log.info("Upserted period %s into master files at %s", period, result)

    if result is None:
        raise PipelineError(
            "No period succeeded - master files were never written. "
            "See the per-period errors above and the diagnostics dumps "
            "under run_data_malawi/screenshots/."
        )

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Malawi LMIS pipeline")
    parser.add_argument("--config", default="config_malawi.yaml", type=Path)
    parser.add_argument(
        "--program",
        default=None,
        help=(
            "Override filters.program_name for this run (e.g. HIV instead of the "
            "default Reproductive Health). This is Malawi's core report parameter "
            "(the Program Name select2), not a separate site-side filter like "
            "Mozambique's - so overriding it needs no scraper changes."
        ),
    )
    parser.add_argument(
        "--master-basename",
        default=None,
        help=(
            "Override output.master_basename for this run (e.g. LMIS_MW_HIV), so a "
            "differently-filtered extraction (see --program) writes to its own "
            "master files instead of the default LMIS_MW.{xlsx,csv}."
        ),
    )
    parser.add_argument(
        "--product-category-file",
        default=None,
        type=Path,
        help=(
            "Override filters.product_category_file for this run - a master "
            "Product/Category list used to both restrict the extract to known "
            "products and populate Category for them via fuzzy matching. See "
            "product_matching.py for how, and its accuracy caveats - not yet "
            "validated against real Malawi HIV product naming."
        ),
    )
    args = parser.parse_args()

    try:
        cfg = Config.load(args.config)
    except ConfigError as e:
        log.error("Config error: %s", e)
        return 1

    if args.program:
        cfg.data.setdefault("filters", {})["program_name"] = args.program
    if args.master_basename:
        cfg.data.setdefault("output", {})["master_basename"] = args.master_basename
    if args.product_category_file:
        cfg.data.setdefault("filters", {})["product_category_file"] = str(args.product_category_file)

    log_path = enable_file_logging(cfg.get("output.log_dir", "./run_data_malawi/logs"))
    log.info("Logging this run to %s", log_path)

    try:
        xlsx_path, csv_path = run(cfg)
    except (PipelineError, ScraperError) as e:
        log.error("Pipeline failed: %s", e)
        return 1

    print(f"Master workbook: {xlsx_path}")
    print(f"Master CSV: {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
