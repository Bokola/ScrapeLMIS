"""End-to-end orchestrator for the SIMAM (LMIS Mozambique) pipeline:

    login -> open Analytics Reports -> Requisition Data Report -> Download
    results (xlsx) -> stage raw -> validate schema -> upsert into master
    workbook -> (optional) reconcile against a manual baseline.

Run with: uv run python -m lmis_pipeline.main [--baseline path/to/manual_download.xlsx]
"""
from __future__ import annotations

import argparse
import calendar
import re
import sys
from pathlib import Path

import pandas as pd

from .config import Config, ConfigError
from .extract import read_downloaded_file, upsert_to_excel_and_csv
from .landing import stage_raw_download
from .logger import enable_file_logging, get_logger
from .periods import month_window
from .schema_validation import LMIS_REQUISITION_SCHEMA, validate_extract
from .scraper import ScraperError, open_browser

log = get_logger(__name__)

# Column translations, Health Category, Product Short, and Category were
# all CONFIRMED by testing directly against a real translated sample file
# (LMIS_MZ.csv) that already contained both the original Portuguese
# columns/values and the target English ones side by side - every mapping
# reproduced the real file's own columns with a 100% match across all
# 1000 sample rows. "Period"'s format was ALSO confirmed that way
# originally, then deliberately changed afterward per explicit request
# (date only, matching Malawi's "01/mm/yyyy", not the datetime format the
# sample file actually used) - see the comment at that line.

# Straight 1:1 header translations. Deliberately excludes "Período" - that
# column is replaced entirely by a freshly-derived "Period" (see below),
# not just renamed, since the derived version is a clean single-month
# value rather than whatever the raw report's own "Período" column held.
COLUMN_TRANSLATIONS = {
    "Província": "Province",
    "Distrito": "District",
    "Tipo de instalação": "Installation type",
    "Código da instalação": "Installation code",
    "Nome da instalação": "Installation Name",
    "Nome do relatório": "Report Name",
    "Período de análise": "Analysis period",
    "Programa": "Program",
    "Código do produto": "Product Code",
    "Nome do produto": "Product Name",
    "Saldo inicial": "Opening balance",
    "Entradas": "Entries",
    "Saídas": "Outputs",
    "Ajustes": "Settings",
    "Empréstimos": "Loans",
    "Stock teórico": "Theoretical stock",
    "Inventário": "Inventory",
    "Valor total": "Total value",
}

# Second header row for the master files, per explicit request, confirmed
# against a real sample file (sample-LMIS_MZ.csv) - the exact reverse of
# COLUMN_TRANSLATIONS, plus "Period" (which maps to "Período" even though
# it's a derived column, not a straight rename of the original "Período" -
# the sample file uses that label for it anyway) and the four derived
# columns mapped to themselves (no Portuguese equivalent exists for them
# in the original report).
SECOND_HEADER_ROW = {en: pt for pt, en in COLUMN_TRANSLATIONS.items()}
SECOND_HEADER_ROW["Period"] = "Período"
for _derived_col in ("Health Category", "Product Short", "Date", "Category"):
    SECOND_HEADER_ROW[_derived_col] = _derived_col

# Health Category, derived from Tipo de instalação: everything is a
# "Health Facility" except a warehouse ("DDM").
INSTALLATION_TYPE_TO_HEALTH_CATEGORY = {"DDM": "Warehouse"}
DEFAULT_HEALTH_CATEGORY = "Health Facility"

# Product Short / Category, derived from Nome do produto by exact match.
# All six entries confirmed against a real sample file (100% match across
# 1000 rows). "Medroxiprogesterona acetato; 5mg; Comp" (the Comp/tablet
# form) is deliberately not tracked - dropped from config.yaml's
# filters.products per explicit request, so it's excluded here too.
PRODUCT_NAME_TO_SHORT_AND_CATEGORY = {
    "Copper T380 dispositivo ; 0,2%; DIU": ("Copper IUD", "IUD"),
    "Etonogestrel ; 68 mg(Implanon NXT); Implante": ("Implanon", "Implants"),
    "Jadelle; 150mg; Implante": ("Jadelle", "Implants"),
    "Levoplant ; 75mg; Implante": ("Levoplant", "Implants"),
    "Medroxiprogesterona (Sayana Press); 104mg/0.65ml; Inj": ("DMPA-SC", "Injectables"),
    "Medroxiprogesterona acetate.; 150mg/mL; Inj": ("DMPA-IM", "Injectables"),
}

# Portuguese month abbreviations as they appear in "Período de análise"
# range strings (e.g. "21 Ago 2024 - 20 Set 2024") -> (month number, full
# English month name).
PT_MONTH_ABBR = {
    "Jan": (1, "January"), "Fev": (2, "February"), "Mar": (3, "March"),
    "Abr": (4, "April"), "Mai": (5, "May"), "Jun": (6, "June"),
    "Jul": (7, "July"), "Ago": (8, "August"), "Set": (9, "September"),
    "Out": (10, "October"), "Nov": (11, "November"), "Dez": (12, "December"),
}
_PT_MONTH_NUM_TO_EN = {num: en for num, en in PT_MONTH_ABBR.values()}

_ANALYSIS_PERIOD_END_RE = re.compile(r"-\s*\d{1,2}\s+([A-Za-z]+)\s+(\d{4})\s*$")


def _parse_period_end_month(analysis_period: str) -> tuple[int, int] | None:
    """Parse '21 Ago 2024 - 20 Set 2024' and return (year, month) of the
    END date, e.g. (2024, 9). None if the string doesn't match."""
    match = _ANALYSIS_PERIOD_END_RE.search(str(analysis_period))
    if not match:
        return None
    month_abbr, year = match.group(1), int(match.group(2))
    entry = PT_MONTH_ABBR.get(month_abbr)
    if entry is None:
        return None
    return year, entry[0]


def filter_to_period_window(
    df: pd.DataFrame, n_months: int, offset_months: int
) -> pd.DataFrame:
    """Keep only rows whose "Período de análise" end-month falls within
    the configured output window (see periods.month_window) - a
    client-side trim, since SIMAM's own period widget only supports a
    single trailing "Previous N months" span ending at the current month,
    not an arbitrary offset/lag. The site is asked for a wide-enough span
    (config's filters.period_months, unchanged) and this narrows the
    downloaded data down to exactly what's wanted afterward - the same
    approach used for Malawi's product filtering, where the site also
    couldn't do the filtering itself.

    A no-op (returns df unchanged) when offset_months is 0, since a
    zero-offset window ending at the current month is exactly what the
    site's own "Previous N months" filter already returns - filtering
    again would be redundant, not incorrect, but skipped for clarity.
    """
    if offset_months == 0:
        return df
    allowed = set(month_window(n_months, offset_months))
    parsed = df["Período de análise"].map(_parse_period_end_month)
    before = len(df)
    filtered = df[parsed.map(lambda ym: ym in allowed)].reset_index(drop=True)
    log.info(
        "Filtered to output period window (%d month(s), offset %d): "
        "%d row(s) -> %d row(s) kept",
        n_months, offset_months, before, len(filtered),
    )
    return filtered


def translate_and_enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Translate the raw report's Portuguese column headers to English and
    add four derived columns (Health Category, Product Short, Category,
    Date), matching the exact target format in LMIS_MZ.csv. Called after
    schema validation passes (validation checks the raw Portuguese
    columns - translating first would require duplicating the schema in
    two languages for no benefit).
    """
    df = df.copy()

    df["Health Category"] = df["Tipo de instalação"].map(
        lambda t: INSTALLATION_TYPE_TO_HEALTH_CATEGORY.get(str(t).strip(), DEFAULT_HEALTH_CATEGORY)
    )

    product_lookup = df["Nome do produto"].map(
        lambda name: PRODUCT_NAME_TO_SHORT_AND_CATEGORY.get(str(name).strip(), (str(name), ""))
    )
    df["Product Short"] = product_lookup.map(lambda t: t[0])
    df["Category"] = product_lookup.map(lambda t: t[1])

    parsed = df["Período de análise"].map(_parse_period_end_month)
    # Date, not datetime, per explicit request - matching Malawi's own
    # "01/mm/yyyy" Period format (day always "01", since these are monthly
    # periods with no real day component). This deliberately overrides the
    # "2024-09-01 00:00:00" datetime format originally confirmed against a
    # real sample file - that format was correct at the time, but the
    # format itself has since changed on request.
    # "Mon-YY" per explicit request (e.g. "Jan-24") - confirmed against a
    # real sample file (sample-LMIS_MZ.csv). This supersedes the earlier
    # "01/mm/yyyy" format, which itself had superseded the original
    # datetime format - each was correct when it was requested; only the
    # format itself keeps changing.
    df["Period"] = parsed.map(
        lambda ym: f"{calendar.month_abbr[ym[1]]}-{str(ym[0])[-2:]}" if ym else ""
    )
    df["Date"] = parsed.map(lambda ym: f"{_PT_MONTH_NUM_TO_EN[ym[1]]}-{ym[0]}" if ym else "")

    if "Período" in df.columns:
        df = df.drop(columns=["Período"])
    df = df.rename(columns=COLUMN_TRANSLATIONS)

    return df


class PipelineError(RuntimeError):
    pass


def run(cfg: Config, baseline_path: Path | None = None) -> tuple[Path, Path]:
    """Run the pipeline once. Returns (xlsx_path, csv_path) for the updated
    master files - both are always written together, see
    extract.upsert_to_excel_and_csv(). Raises PipelineError on a schema
    validation failure (per config's validation.fail_on_error) - the raw
    landing capture is preserved either way, so nothing is lost even on a
    hard stop.
    """
    download_dir = cfg.get("output.download_dir", "./run_data/downloads")

    with open_browser(cfg) as s:
        s.ensure_logged_in()
        s.set_language_english()
        try:
            s.open_requisition_report()
            s.set_product_filter(cfg.get("filters.products", []))
            s.set_period_filter(cfg.get("filters.period_months"))
            downloaded_path = s.download_results_xlsx(download_dir)
        except ScraperError as e:
            raise PipelineError(f"Scraping the Requisition Data Report failed: {e}") from e

    landing_path = stage_raw_download(downloaded_path, cfg)
    log.info("Staged raw download to %s", landing_path)

    df = read_downloaded_file(downloaded_path)

    validation = validate_extract(df, schema=LMIS_REQUISITION_SCHEMA)
    if not validation.ok:
        log.warning(validation.summary())
        if cfg.get("validation.fail_on_error", True):
            raise PipelineError(
                f"{validation.summary()} Raw payload preserved at "
                f"{landing_path} for diagnosis - re-run against it once fixed."
            )

    df = filter_to_period_window(
        df,
        n_months=cfg.get("filters.output_period_count", 4),
        offset_months=cfg.get("filters.output_period_offset", 0),
    )
    df_translated = translate_and_enrich(df)
    xlsx_path, csv_path = upsert_to_excel_and_csv(
        df_translated, cfg, second_header=SECOND_HEADER_ROW
    )
    log.info("Pipeline complete: master files at %s and %s", xlsx_path, csv_path)

    if baseline_path is not None:
        from .reconciliation import load_baseline, reconcile, write_reconciliation_report

        # Reconciliation deliberately uses the RAW (untranslated) df, not
        # df_translated - a manually downloaded baseline is a direct export
        # from SIMAM's own UI, so it's in the same raw Portuguese columns
        # config.yaml's reconciliation.kpi_columns/group_by already expect
        # (e.g. "Stock teórico", "Província").
        baseline_df = load_baseline(baseline_path)
        report = reconcile(
            df,
            baseline_df,
            kpi_columns=cfg.get("reconciliation.kpi_columns", []),
            group_by=cfg.get("reconciliation.group_by"),
            tolerance_pct=cfg.get("reconciliation.tolerance_pct", 1.0),
        )
        report_path = Path(cfg.get("output.dir", "./run_data/extracts")) / "reconciliation_report.xlsx"
        write_reconciliation_report(report, report_path)
        if not report.ok:
            log.warning("Reconciliation did not pass - see %s", report_path)

    return xlsx_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the SIMAM (LMIS Mozambique) Requisition Data Report pipeline")
    parser.add_argument("--config", default="config.yaml", type=Path)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Path to a manually-downloaded baseline (.xlsx/.csv) to reconcile against",
    )
    args = parser.parse_args()

    try:
        cfg = Config.load(args.config)
    except ConfigError as e:
        log.error("Config error: %s", e)
        return 1

    log_path = enable_file_logging(cfg.get("output.log_dir", "./run_data/logs"))
    log.info("Logging this run to %s", log_path)

    try:
        xlsx_path, csv_path = run(cfg, baseline_path=args.baseline)
    except (PipelineError, ScraperError) as e:
        log.error("Pipeline failed: %s", e)
        return 1

    print(f"Master workbook: {xlsx_path}")
    print(f"Master CSV: {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
