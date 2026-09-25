"""Turn the raw downloaded requisition file into a DataFrame and upsert it
into a persistent master workbook.

Full pipeline order (see run_full_extraction in main.py): scraper downloads
the file -> stage it to the landing zone untouched -> read it into a
DataFrame -> validate structure -> upsert into the master .xlsx. Staging
happens BEFORE validation on purpose - even a run that fails validation
leaves a raw capture on disk to diagnose against.
"""
from __future__ import annotations

import csv
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


def _write_formatted_excel(
    df: pd.DataFrame,
    path: Path,
    sheet_name: str,
    second_header: dict[str, str] | None = None,
    text_format_columns: list[str] | None = None,
) -> None:
    """Shared formatting: bold+frozen header row(s), auto-sized columns.

    second_header, if given, maps each column name to a second label
    (e.g. a translation) inserted as its own bold row directly below the
    real header row - both rows are then frozen together. Used for
    Mozambique's dual English/Portuguese header requirement; None
    (default) writes a single header row exactly as before, which is what
    Malawi still uses.

    text_format_columns, if given, forces every cell in those columns to
    Excel's Text number format ("@"). Confirmed real-world problem this
    solves: a plain string like "Jan-24" is written correctly as text, but
    Excel's own auto-detection can still silently reinterpret it as a real
    date on open (showing "Jan-24" on the surface, while the cell's actual
    underlying value becomes a full date like "01/01/2024", visible in the
    formula bar on click) - this is Excel's own behavior, not something
    controllable from the data itself, EXCEPT by explicitly locking the
    cell's format to Text, which this parameter does. Both country
    pipelines apply this to their "Period" column.
    """
    if df.empty:
        log.warning("DataFrame is empty - writing a header-only workbook to %s", path)

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name=sheet_name, index=False)
        worksheet = writer.sheets[sheet_name]

        header_rows = 1
        if second_header:
            worksheet.insert_rows(2)
            for col_idx, column in enumerate(df.columns, start=1):
                cell = worksheet.cell(row=2, column=col_idx)
                cell.value = second_header.get(column, column)
                cell.font = cell.font.copy(bold=True)
            header_rows = 2

        for cell in worksheet[1]:
            cell.font = cell.font.copy(bold=True)
        worksheet.freeze_panes = f"A{header_rows + 1}"

        if text_format_columns:
            text_col_indices = {
                i for i, c in enumerate(df.columns, start=1) if c in text_format_columns
            }
            if text_col_indices:
                for row in worksheet.iter_rows(
                    min_row=header_rows + 1, max_row=worksheet.max_row
                ):
                    for cell in row:
                        if cell.column in text_col_indices:
                            cell.number_format = "@"

        for col_idx, column in enumerate(df.columns, start=1):
            lengths = [len(str(column))]
            if second_header:
                lengths.append(len(str(second_header.get(column, column))))
            if len(df):
                lengths.extend(len(str(v)) for v in df[column].astype(str))
            max_len = max(lengths)
            col_letter = worksheet.cell(row=1, column=col_idx).column_letter
            worksheet.column_dimensions[col_letter].width = min(max_len + 2, 60)


def upsert_to_excel_and_csv(
    df_new: pd.DataFrame,
    cfg: Config,
    second_header: dict[str, str] | None = None,
    text_format_columns: list[str] | None = None,
) -> tuple[Path, Path]:
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

    second_header, if given, maps each column name to a second label
    (e.g. a translation) written as its own row directly below the real
    header row, in BOTH files - per Mozambique's dual English/Portuguese
    header requirement. None (default, what Malawi uses) writes a single
    header row exactly as before. When given, that second row is always
    skipped again when reading either file back in on a later run (so it
    never gets treated as a real data row and silently accumulated).

    text_format_columns, if given, forces the .xlsx cells in those columns
    to Excel's Text format, so Excel can't silently reinterpret a
    date-like string (e.g. "Jan-24") as a real date on open - see
    _write_formatted_excel()'s own docstring for the full explanation.
    Only affects the .xlsx; CSV has no cell-format metadata at all, so
    this can't be applied there - opening the CSV directly in Excel may
    still show this same reinterpretation, unavoidably.

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
    csv_path = out_dir / f"{base_name}_auto.csv"  # "_auto" per explicit request, applies to
                                                    # both countries since this function is shared

    df_new = _reorder_columns(df_new, excel_columns)
    skip_second_row = [1] if second_header else None

    existing = None
    if xlsx_path.exists():
        existing = pd.read_excel(xlsx_path, sheet_name=sheet_name, skiprows=skip_second_row)
    elif csv_path.exists():
        existing = pd.read_csv(csv_path, dtype=str, skiprows=skip_second_row)

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
    _write_formatted_excel(
        combined, xlsx_path, sheet_name, second_header=second_header,
        text_format_columns=text_format_columns,
    )
    _write_csv_with_second_header(combined, csv_path, second_header=second_header)
    log.info("Wrote %d total row(s) to %s and %s", len(combined), xlsx_path, csv_path)
    return xlsx_path, csv_path


def _write_csv_with_second_header(
    df: pd.DataFrame, path: Path, second_header: dict[str, str] | None = None
) -> None:
    """Write df to CSV, with an optional second header row (see
    upsert_to_excel_and_csv's second_header) directly below the real
    header row. None writes a plain single-header CSV exactly as before.
    """
    if not second_header:
        df.to_csv(path, index=False)
        return

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(list(df.columns))
        writer.writerow([second_header.get(c, c) for c in df.columns])
        df.to_csv(f, index=False, header=False)


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
