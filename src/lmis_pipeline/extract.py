"""Turn the raw downloaded requisition file into a DataFrame and upsert it
into a persistent master workbook.

Full pipeline order (see run_full_extraction in main.py): scraper downloads
the file -> stage it to the landing zone untouched -> read it into a
DataFrame -> validate structure -> upsert into the master .xlsx. Staging
happens BEFORE validation on purpose - even a run that fails validation
leaves a raw capture on disk to diagnose against.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .config import Config
from .logger import get_logger

log = get_logger(__name__)


def read_downloaded_file(path: str | Path, sheet_name: str | int = 0) -> pd.DataFrame:
    """Read the downloaded results file (.xlsx or .csv - SIMAM's menu
    offers both) into a DataFrame. Everything is read as string first, same
    as the landing-zone convention elsewhere in this style of pipeline -
    typing/cleanup is schema_validation's job, not this step's."""
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path, sheet_name=sheet_name)
    elif path.suffix.lower() == ".csv":
        df = pd.read_csv(path)
    else:
        raise ValueError(f"Unrecognized downloaded file extension: {path.suffix}")
    log.info("Read %d rows, %d columns from %s", len(df), len(df.columns), path)
    return df


def _reorder_columns(df: pd.DataFrame, excel_columns: list[str]) -> pd.DataFrame:
    """Put columns in the order given by config's excel_columns, dropping
    ones that aren't present and appending anything extra at the end (so a
    new SIMAM column never silently disappears - it just lands last until
    you add it to excel_columns)."""
    if not excel_columns or df.empty:
        return df
    ordered = [c for c in excel_columns if c in df.columns]
    extra = [c for c in df.columns if c not in excel_columns]
    return df[ordered + extra]


def _write_formatted_excel(df: pd.DataFrame, path: Path, sheet_name: str) -> None:
    """Shared formatting: bold+frozen header row, auto-sized columns."""
    if df.empty:
        log.warning("DataFrame is empty - writing a header-only workbook to %s", path)

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name=sheet_name, index=False)
        worksheet = writer.sheets[sheet_name]

        for cell in worksheet[1]:
            cell.font = cell.font.copy(bold=True)
        worksheet.freeze_panes = "A2"

        for col_idx, column in enumerate(df.columns, start=1):
            max_len = max(
                [len(str(column))] + [len(str(v)) for v in df[column].astype(str)]
            ) if len(df) else len(str(column))
            col_letter = worksheet.cell(row=1, column=col_idx).column_letter
            worksheet.column_dimensions[col_letter].width = min(max_len + 2, 60)


def upsert_to_excel_and_csv(df_new: pd.DataFrame, cfg: Config) -> tuple[Path, Path]:
    """Write df_new into persistent master files in BOTH .xlsx and .csv
    formats, updating existing rows in place (matched on config's
    dedup_key) rather than duplicating them.

    Both files are always kept in sync: the merged result is computed
    once, then written to both formats, rather than upserting each format
    independently - independent upserts could let the two files silently
    drift apart over time (e.g. if one write step failed but not the
    other, or if they were run at different times against different
    versions of the code).

    File names come from config's output.master_basename (e.g. "LMIS_MZ",
    "LMIS_MW") with .xlsx/.csv appended, per explicit request - not
    output.master_filename, which named a single file with its own
    extension.

    The existing merged state is read from whichever of the two files
    exists (preferring .xlsx if both do, since read_excel's dtype
    handling is a bit more precise than a round-tripped CSV) - if only one
    exists (e.g. a master file from before this dual-format convention),
    that one is used and both are (re)written from here on.
    """
    out_dir = Path(cfg.get("output.dir", "./run_data/extracts"))
    out_dir.mkdir(parents=True, exist_ok=True)
    base_name = cfg.get("output.master_basename", "LMIS")
    sheet_name = cfg.get("output.sheet_name", "Sheet1")
    excel_columns = cfg.get("excel_columns", [])
    dedup_key = cfg.get("dedup_key", [])

    xlsx_path = out_dir / f"{base_name}.xlsx"
    csv_path = out_dir / f"{base_name}.csv"

    df_new = _reorder_columns(df_new, excel_columns)

    existing = None
    if xlsx_path.exists():
        existing = pd.read_excel(xlsx_path, sheet_name=sheet_name)
    elif csv_path.exists():
        existing = pd.read_csv(csv_path, dtype=str)

    if existing is not None:
        existing = _reorder_columns(existing, excel_columns)
        key_cols_present = (
            dedup_key
            and all(k in existing.columns for k in dedup_key)
            and all(k in df_new.columns for k in dedup_key)
        )
        if key_cols_present:
            combined = pd.concat([existing, df_new], ignore_index=True)
            before = len(combined)
            # keep="last" -> the new extraction's values win on a key clash
            combined = combined.drop_duplicates(subset=dedup_key, keep="last")
            log.info(
                "Upsert: %d existing + %d new rows -> %d after dedup on %s "
                "(%d row(s) updated in place)",
                len(existing), len(df_new), len(combined),
                dedup_key, before - len(combined),
            )
        else:
            log.warning(
                "dedup_key %s not fully present in both existing and new "
                "data - appending without dedup instead of upserting",
                dedup_key,
            )
            combined = pd.concat([existing, df_new], ignore_index=True)
    else:
        combined = df_new
        log.info("No existing master files at %s.{xlsx,csv} - creating them fresh", out_dir / base_name)

    combined = _reorder_columns(combined, excel_columns)
    _write_formatted_excel(combined, xlsx_path, sheet_name)
    combined.to_csv(csv_path, index=False)
    log.info("Wrote %d total row(s) to %s and %s", len(combined), xlsx_path, csv_path)
    return xlsx_path, csv_path


def upsert_to_csv(df_new: pd.DataFrame, cfg: Config) -> Path:
    """Write df_new into a persistent master CSV, updating existing rows in
    place rather than duplicating them - matched on config's dedup_key.
    Mirrors upsert_to_excel()'s dedup logic exactly, but for a plain CSV
    master file instead of a formatted .xlsx workbook (no bold header,
    frozen panes, or column widths - CSV has no such concept).

    Existing rows are read back with dtype=str (not pandas' inferred
    types) so repeated upsert cycles don't introduce inconsistencies like
    "1.0" appearing after a column picks up a stray NaN and gets upcast to
    float - matches this project's general "treat scraped/downloaded
    values as text" convention elsewhere (e.g. landing.py's parquet
    staging).
    """
    out_dir = Path(cfg.get("output.dir", "./run_data/extracts"))
    out_dir.mkdir(parents=True, exist_ok=True)
    master_path = out_dir / cfg.get("output.master_filename", "master.csv")
    excel_columns = cfg.get("excel_columns", [])
    dedup_key = cfg.get("dedup_key", [])

    df_new = _reorder_columns(df_new, excel_columns)

    if master_path.exists():
        existing = pd.read_csv(master_path, dtype=str)
        existing = _reorder_columns(existing, excel_columns)

        key_cols_present = (
            dedup_key
            and all(k in existing.columns for k in dedup_key)
            and all(k in df_new.columns for k in dedup_key)
        )
        if key_cols_present:
            combined = pd.concat([existing, df_new.astype(str)], ignore_index=True)
            before = len(combined)
            # keep="last" -> the new extraction's values win on a key clash
            combined = combined.drop_duplicates(subset=dedup_key, keep="last")
            log.info(
                "Upsert: %d existing + %d new rows -> %d after dedup on %s "
                "(%d row(s) updated in place)",
                len(existing), len(df_new), len(combined),
                dedup_key, before - len(combined),
            )
        else:
            log.warning(
                "dedup_key %s not fully present in both existing and new "
                "data - appending without dedup instead of upserting",
                dedup_key,
            )
            combined = pd.concat([existing, df_new.astype(str)], ignore_index=True)
    else:
        combined = df_new.astype(str)
        log.info("No existing master CSV at %s - creating it fresh", master_path)

    combined = _reorder_columns(combined, excel_columns)
    combined.to_csv(master_path, index=False)
    log.info("Wrote %d total row(s) to master CSV %s", len(combined), master_path)
    return master_path


def upsert_to_excel(df_new: pd.DataFrame, cfg: Config) -> Path:
    """Write df_new into the persistent master workbook, updating existing
    rows in place rather than duplicating them - matched on config's
    dedup_key (facility + product + period).

    If the master workbook doesn't exist yet, it's created fresh. If
    dedup_key columns aren't all present in both the existing workbook and
    df_new, falls back to a plain append (with a warning) rather than
    silently dropping rows.
    """
    out_dir = Path(cfg.get("output.dir", "./run_data/extracts"))
    out_dir.mkdir(parents=True, exist_ok=True)
    master_path = out_dir / cfg.get("output.master_filename", "lmis_mz_master.xlsx")
    sheet_name = cfg.get("output.sheet_name", "Query result")
    excel_columns = cfg.get("excel_columns", [])
    dedup_key = cfg.get("dedup_key", [])

    df_new = _reorder_columns(df_new, excel_columns)

    if master_path.exists():
        existing = pd.read_excel(master_path, sheet_name=sheet_name)
        existing = _reorder_columns(existing, excel_columns)

        key_cols_present = (
            dedup_key
            and all(k in existing.columns for k in dedup_key)
            and all(k in df_new.columns for k in dedup_key)
        )
        if key_cols_present:
            combined = pd.concat([existing, df_new], ignore_index=True)
            before = len(combined)
            # keep="last" -> the new extraction's values win on a key clash
            combined = combined.drop_duplicates(subset=dedup_key, keep="last")
            log.info(
                "Upsert: %d existing + %d new rows -> %d after dedup on %s "
                "(%d row(s) updated in place)",
                len(existing), len(df_new), len(combined),
                dedup_key, before - len(combined),
            )
        else:
            log.warning(
                "dedup_key %s not fully present in both existing and new "
                "data - appending without dedup instead of upserting",
                dedup_key,
            )
            combined = pd.concat([existing, df_new], ignore_index=True)
    else:
        combined = df_new
        log.info("No existing master workbook at %s - creating it fresh", master_path)

    combined = _reorder_columns(combined, excel_columns)
    _write_formatted_excel(combined, master_path, sheet_name)
    return master_path
