"""Resolve rows that share a dedup key (facility + product + period) before
they reach the master file.

SIMAM's Requisition Data Report can return more than one row for the same
facility, product and month (confirmed on the Jul to Sep 2026 MZ data: 12
keys, two rows each). Without this step those rows either all land in the
master file (when no master exists yet, the upsert has nothing to dedup
against) or one of them is kept arbitrarily by drop_duplicates(keep="last").

Three cases, decided per key:
  exact copy         every column identical, one row kept
  zero row           extra rows are zero in every measure column while
                     another row has values, zero rows dropped (summing
                     would give the same numbers, so nothing changes)
  conflict           rows disagree on the quantities, nobody here can tell
                     which is right; handled per duplicates.on_conflict

Everything removed or held back is written to a csv under
duplicates.report_dir so it can be raised with the source.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from .logger import get_logger

log = get_logger(__name__)

VALID_ON_CONFLICT = ("exclude", "fail")


class DuplicateConflictError(RuntimeError):
    """Raised when duplicates.on_conflict is "fail" and conflicting rows exist."""


def _measure_is_zero(df: pd.DataFrame, measure_columns: list[str]) -> pd.Series:
    # blank or non numeric counts as zero, same as the lakehouse pipeline does
    cols = [c for c in measure_columns if c in df.columns]
    if not cols:
        raise ValueError(f"none of duplicates.measure_columns {measure_columns} are in the data")
    numeric = df[cols].apply(pd.to_numeric, errors="coerce").fillna(0)
    return (numeric == 0).all(axis=1)


def resolve_duplicate_keys(
    df: pd.DataFrame,
    key: list[str],
    measure_columns: list[str],
    label: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return (clean_df, removed_rows, conflict_rows).

    clean_df still contains the conflicting keys; the caller decides what to
    do with them (see apply_duplicate_policy). removed_rows and
    conflict_rows carry a _reason column for the report.
    """
    if df.empty:
        empty = df.iloc[0:0].assign(_reason=pd.Series(dtype=str))
        return df, empty, empty

    removed: list[pd.DataFrame] = []

    # exact copies across every column
    exact = df.duplicated(keep="first")
    if exact.any():
        removed.append(df[exact].assign(_reason="exact copy"))
    df = df[~exact]

    # zero rows next to a row with values for the same key
    zero = _measure_is_zero(df, measure_columns)
    size = df.groupby(key, dropna=False)[key[0]].transform("size")
    nonzero_in_key = (~zero).groupby([df[k] for k in key], dropna=False).transform("sum")
    drop_zero = (size > 1) & zero & (nonzero_in_key >= 1)
    if drop_zero.any():
        removed.append(df[drop_zero].assign(_reason="zero row beside a row with values"))
    df = df[~drop_zero]

    # keys where every row is zero but other columns differ, keep the first
    zero = _measure_is_zero(df, measure_columns)
    all_zero_extra = df.duplicated(subset=key, keep="first") & zero
    all_zero_key = zero.groupby([df[k] for k in key], dropna=False).transform("all")
    drop_all_zero = all_zero_extra & all_zero_key
    if drop_all_zero.any():
        removed.append(df[drop_all_zero].assign(_reason="all zero, extra row"))
    df = df[~drop_all_zero]

    # whatever still repeats a key is a real conflict
    conflict_mask = df.duplicated(subset=key, keep=False)
    conflicts = df[conflict_mask].assign(_reason="conflict: quantities differ")

    removed_df = (pd.concat(removed, ignore_index=True) if removed
                  else df.iloc[0:0].assign(_reason=pd.Series(dtype=str)))
    n_conflict_keys = conflicts.drop_duplicates(subset=key).shape[0]
    if len(removed_df) or n_conflict_keys:
        log.warning(
            "%s: duplicate keys on %s -- %d exact copy row(s) and %d zero row(s) dropped, "
            "%d key(s) in conflict (%d row(s))",
            label, key,
            int((removed_df["_reason"] == "exact copy").sum()),
            int((removed_df["_reason"] != "exact copy").sum()),
            n_conflict_keys, len(conflicts),
        )
    return df.reset_index(drop=True), removed_df, conflicts


def apply_duplicate_policy(
    df_new: pd.DataFrame,
    existing: pd.DataFrame | None,
    key: list[str],
    policy: dict,
) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    """Clean the new download and the existing master before they are merged.

    Existing rows whose key is in the new download (clean or conflicting)
    are dropped first, since the new download supersedes them anyway; the
    rest of the existing master is then cleaned the same way, so duplicates
    already written by earlier runs are fixed too.

    on_conflict "exclude": conflicting keys are left out of the master (from
    both the new download and any older copy), listed in the report and
    logged. on_conflict "fail": the report is written and
    DuplicateConflictError is raised; nothing is written to the master.
    """
    measure_columns = list(policy.get("measure_columns") or [])
    on_conflict = policy.get("on_conflict", "fail")
    if on_conflict not in VALID_ON_CONFLICT:
        raise ValueError(f"duplicates.on_conflict must be one of {VALID_ON_CONFLICT}, got {on_conflict!r}")
    if not measure_columns:
        raise ValueError("duplicates.measure_columns must list the quantity columns")

    reports: list[pd.DataFrame] = []

    df_new, removed_new, conflicts_new = resolve_duplicate_keys(df_new, key, measure_columns, "new download")
    reports += [removed_new.assign(_source="new download"), conflicts_new.assign(_source="new download")]
    conflict_keys = conflicts_new[key].drop_duplicates()

    if existing is not None and not existing.empty:
        new_keys = pd.concat([df_new[key], conflict_keys]).drop_duplicates()
        superseded = existing.set_index(key).index.isin(new_keys.set_index(key).index)
        existing = existing[~superseded]
        existing, removed_ex, conflicts_ex = resolve_duplicate_keys(existing, key, measure_columns, "existing master")
        reports += [removed_ex.assign(_source="existing master"), conflicts_ex.assign(_source="existing master")]
        conflict_keys = pd.concat([conflict_keys, conflicts_ex[key]]).drop_duplicates()

    report = pd.concat([r for r in reports if not r.empty], ignore_index=True) if any(
        not r.empty for r in reports) else None
    if report is not None:
        report_dir = Path(policy.get("report_dir", "./run_data/duplicates"))
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"duplicates_{datetime.now():%Y%m%d_%H%M%S}.csv"
        cols = ["_source", "_reason"] + [c for c in report.columns if c not in ("_source", "_reason")]
        report[cols].to_csv(report_path, index=False, encoding="utf-8-sig")
        log.warning("duplicate rows report written to %s", report_path)

    if not conflict_keys.empty:
        sample = [tuple(r) for r in conflict_keys.head(10).itertuples(index=False)]
        if on_conflict == "fail":
            raise DuplicateConflictError(
                f"{len(conflict_keys)} key(s) {key} have rows with different quantities, "
                f"master file not written (see the duplicates report). e.g. {sample}")
        conflict_index = conflict_keys.set_index(key).index
        df_new = df_new[~df_new.set_index(key).index.isin(conflict_index)]
        if existing is not None:
            existing = existing[~existing.set_index(key).index.isin(conflict_index)]
        log.warning(
            "%d conflicting key(s) left OUT of the master file (duplicates.on_conflict = exclude), "
            "raise them with the data source: %s",
            len(conflict_keys), sample,
        )

    return df_new.reset_index(drop=True), (existing.reset_index(drop=True) if existing is not None else None)
