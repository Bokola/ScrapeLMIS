"""Schema validation: structural checks run on the downloaded requisition
extract BEFORE it is upserted into the master workbook.

The failure mode this guards against isn't "the browser crashed" - it's the
quiet one: SIMAM renames or reorders a report column, a facility's row comes
back with a blank product/period, or the "Download results" menu silently
hands back a different report than the one on screen. Those all produce a
file that opens fine in Excel and would otherwise land in the master
workbook unnoticed. This step turns that into a loud, catchable failure.

Requires: pip install pandera  (or: uv add pandera)
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
import pandera.pandas as pa
from pandera.pandas import Check, Column, DataFrameSchema

from .logger import get_logger

log = get_logger(__name__)


# Columns every requisition extract must have, non-blank, matching the
# confirmed real export (LMIS_MZ_2026_04.xlsx). strict=False means extra
# columns are allowed - this validates completeness of the fields the
# pipeline depends on (dedup key + core identifiers), not an exact column
# count, so an added SIMAM column doesn't itself trip a validation failure.
LMIS_REQUISITION_SCHEMA = DataFrameSchema(
    {
        "Província": Column(str, Check.str_length(min_value=1), nullable=False),
        "Código da instalação": Column(str, Check.str_length(min_value=1), nullable=False),
        "Nome da instalação": Column(str, Check.str_length(min_value=1), nullable=False),
        "Código do produto": Column(str, Check.str_length(min_value=1), nullable=False),
        "Nome do produto": Column(str, Check.str_length(min_value=1), nullable=False),
        "Período de análise": Column(str, Check.str_length(min_value=1), nullable=False),
    },
    strict=False,
    coerce=True,  # facility/product codes can come back as numeric-looking strings
)


@dataclass
class ValidationResult:
    ok: bool
    errors: pd.DataFrame | None = None  # pandera's failure_cases table, if any
    blank_field_counts: dict = field(default_factory=dict)
    missing_columns: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            return "Validation passed."
        parts = []
        if self.missing_columns:
            parts.append(f"missing columns: {self.missing_columns}")
        if self.blank_field_counts:
            parts.append(f"blank field counts: {self.blank_field_counts}")
        if self.errors is not None and not self.errors.empty:
            parts.append(f"{len(self.errors)} schema check failure(s)")
        return "Validation failed - " + "; ".join(parts)


def validate_extract(
    df: pd.DataFrame, schema: DataFrameSchema = LMIS_REQUISITION_SCHEMA
) -> ValidationResult:
    """Run structural checks against df. Returns a ValidationResult rather
    than raising, so the caller decides whether to halt the pipeline or
    quarantine-and-continue. Only raises for a genuinely malformed input."""
    if df is None:
        raise ValueError("validate_extract() received None, not a DataFrame")

    missing_columns = [c for c in schema.columns if c not in df.columns]

    if df.empty:
        log.warning("Extract is empty - nothing to validate")
        return ValidationResult(
            ok=False, missing_columns=missing_columns or ["<all - dataframe is empty>"]
        )

    # Blank-but-present values are the classic sign of a silent report
    # change: the column still exists, but the field inside it came back
    # empty. Reported for every object column, not just schema ones.
    blank_counts: dict[str, int] = {}
    for col in df.columns:
        if df[col].dtype == object:
            blank = df[col].astype(str).str.strip().eq("").sum()
            if blank > 0:
                blank_counts[col] = int(blank)

    validatable_cols = [c for c in schema.columns if c in df.columns]
    if not validatable_cols:
        return ValidationResult(
            ok=False, missing_columns=missing_columns, blank_field_counts=blank_counts
        )

    try:
        partial_schema = schema.remove_columns(
            [c for c in schema.columns if c not in validatable_cols]
        )
        partial_schema.validate(df, lazy=True)
        ok = not missing_columns and not blank_counts
        return ValidationResult(
            ok=ok, missing_columns=missing_columns, blank_field_counts=blank_counts
        )
    except pa.errors.SchemaErrors as e:
        log.error("Schema validation failed: %d failure case(s)", len(e.failure_cases))
        return ValidationResult(
            ok=False,
            errors=e.failure_cases,
            missing_columns=missing_columns,
            blank_field_counts=blank_counts,
        )
