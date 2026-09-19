"""Quality reconciliation: compare an automated extract against a manually
downloaded baseline (a human pulling the same Requisition Data Report from
the SIMAM UI by hand) to catch silent extraction drift - the download
picking up a stale/cached report, a menu click landing on the wrong format,
or SIMAM's own report scope changing.

This is a deliberate, periodic check (e.g. run weekly against a fresh manual
download), not something that runs automatically after every scrape - a
manual baseline isn't available every run.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .logger import get_logger

log = get_logger(__name__)


@dataclass
class ReconciliationReport:
    row_count_automated: int
    row_count_baseline: int
    row_count_diff: int
    row_count_diff_pct: float
    row_count_ok: bool
    kpi_results: pd.DataFrame

    @property
    def kpi_ok(self) -> bool:
        return bool(self.kpi_results["ok"].all()) if not self.kpi_results.empty else True

    @property
    def ok(self) -> bool:
        return self.row_count_ok and self.kpi_ok


def load_baseline(path: str | Path) -> pd.DataFrame:
    """Load a manually downloaded baseline file (.xlsx or .csv)."""
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xls"):
        return pd.read_excel(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    raise ValueError(f"Unsupported baseline file type: {path.suffix}")


def _to_numeric(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype(str).str.replace(",", "", regex=False), errors="coerce")


def compare_row_counts(
    df_automated: pd.DataFrame,
    df_baseline: pd.DataFrame,
    tolerance_pct: float = 1.0,
) -> tuple[int, int, int, float, bool]:
    n_auto = len(df_automated)
    n_base = len(df_baseline)
    diff = n_auto - n_base
    if n_base:
        diff_pct = abs(diff) / n_base * 100
    else:
        diff_pct = 100.0 if n_auto else 0.0
    ok = diff_pct <= tolerance_pct
    return n_auto, n_base, diff, diff_pct, ok


def compare_kpis(
    df_automated: pd.DataFrame,
    df_baseline: pd.DataFrame,
    kpi_columns: list[str],
    group_by: str | None = None,
    tolerance_pct: float = 1.0,
) -> pd.DataFrame:
    """Cross-check numeric KPI columns between the two sources. With
    group_by (e.g. 'Country Name'), compares sums per group; without it,
    compares overall totals. Returns one row per (group, kpi) with both
    values, the % difference, and a pass/fail flag.
    """
    results = []

    if group_by:
        auto_groups = set(df_automated.get(group_by, pd.Series(dtype=str)).dropna())
        base_groups = set(df_baseline.get(group_by, pd.Series(dtype=str)).dropna())
        groups = sorted(auto_groups | base_groups)
    else:
        groups = [None]

    for grp in groups:
        auto_slice = df_automated[df_automated[group_by] == grp] if grp is not None else df_automated
        base_slice = df_baseline[df_baseline[group_by] == grp] if grp is not None else df_baseline

        for kpi in kpi_columns:
            auto_val = _to_numeric(auto_slice[kpi]).sum() if kpi in auto_slice.columns else None
            base_val = _to_numeric(base_slice[kpi]).sum() if kpi in base_slice.columns else None

            if auto_val is None or base_val is None:
                diff_pct = None
                ok = False
            else:
                diff = auto_val - base_val
                diff_pct = (abs(diff) / base_val * 100) if base_val else (100.0 if auto_val else 0.0)
                ok = diff_pct <= tolerance_pct

            results.append(
                {
                    "group": grp if grp is not None else "ALL",
                    "kpi": kpi,
                    "automated_value": auto_val,
                    "baseline_value": base_val,
                    "diff_pct": round(diff_pct, 2) if diff_pct is not None else None,
                    "ok": ok,
                }
            )

    return pd.DataFrame(results)


def reconcile(
    df_automated: pd.DataFrame,
    df_baseline: pd.DataFrame,
    kpi_columns: list[str],
    group_by: str | None = None,
    tolerance_pct: float = 1.0,
) -> ReconciliationReport:
    n_auto, n_base, diff, diff_pct, row_count_ok = compare_row_counts(
        df_automated, df_baseline, tolerance_pct=tolerance_pct
    )
    kpi_df = compare_kpis(
        df_automated, df_baseline, kpi_columns, group_by=group_by, tolerance_pct=tolerance_pct
    )

    report = ReconciliationReport(
        row_count_automated=n_auto,
        row_count_baseline=n_base,
        row_count_diff=diff,
        row_count_diff_pct=round(diff_pct, 2),
        row_count_ok=row_count_ok,
        kpi_results=kpi_df,
    )

    if report.ok:
        log.info(
            "Reconciliation PASSED: %d automated vs %d baseline rows (%.2f%% diff)",
            n_auto, n_base, diff_pct,
        )
    else:
        failed_kpis = int((~kpi_df["ok"]).sum()) if not kpi_df.empty else 0
        log.warning(
            "Reconciliation FAILED: %d automated vs %d baseline rows (%.2f%% diff), "
            "%d/%d KPI checks failed",
            n_auto, n_base, diff_pct, failed_kpis, len(kpi_df),
        )
    return report


def write_reconciliation_report(report: ReconciliationReport, path: str | Path) -> Path:
    """Write reconciliation results to an .xlsx: a one-row summary sheet
    plus a per-KPI detail sheet."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    summary = pd.DataFrame(
        [
            {
                "check": "row_count",
                "automated": report.row_count_automated,
                "baseline": report.row_count_baseline,
                "diff": report.row_count_diff,
                "diff_pct": report.row_count_diff_pct,
                "ok": report.row_count_ok,
            },
            {
                "check": "all_kpis",
                "automated": None,
                "baseline": None,
                "diff": None,
                "diff_pct": None,
                "ok": report.kpi_ok,
            },
        ]
    )

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        report.kpi_results.to_excel(writer, sheet_name="KPI Detail", index=False)

    log.info("Wrote reconciliation report to %s", path)
    return path
