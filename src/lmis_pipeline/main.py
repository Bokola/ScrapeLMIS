"""End-to-end orchestrator for the SIMAM (LMIS Mozambique) pipeline:

    login -> open Analytics Reports -> Requisition Data Report -> Download
    results (xlsx) -> stage raw -> validate schema -> upsert into master
    workbook -> (optional) reconcile against a manual baseline.

Run with: uv run python -m lmis_pipeline.main [--baseline path/to/manual_download.xlsx]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config, ConfigError
from .extract import read_downloaded_file, upsert_to_excel
from .landing import stage_raw_download
from .logger import enable_file_logging, get_logger
from .schema_validation import LMIS_REQUISITION_SCHEMA, validate_extract
from .scraper import ScraperError, open_browser

log = get_logger(__name__)


class PipelineError(RuntimeError):
    pass


def run(cfg: Config, baseline_path: Path | None = None) -> Path:
    """Run the pipeline once. Returns the path to the updated master
    workbook. Raises PipelineError on a schema validation failure (per
    config's validation.fail_on_error) - the raw landing capture is
    preserved either way, so nothing is lost even on a hard stop.
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

    master_path = upsert_to_excel(df, cfg)
    log.info("Pipeline complete: master workbook at %s", master_path)

    if baseline_path is not None:
        from .reconciliation import load_baseline, reconcile, write_reconciliation_report

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

    return master_path


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
        master_path = run(cfg, baseline_path=args.baseline)
    except (PipelineError, ScraperError) as e:
        log.error("Pipeline failed: %s", e)
        return 1

    print(f"Master workbook: {master_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
