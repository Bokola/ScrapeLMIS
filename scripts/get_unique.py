# /// script
# dependencies = [
#   "pandas",
#   "openpyxl",
# ]
# ///

from pathlib import Path
import pandas as pd


def print_unique_column_values(
    file_path: str | Path = Path("./run_data/extracts"),
    file_name: str = "lmis_mz_master.xlsx",
    column_name: str = "Nome do produto",
    group_column: str | None = None,
) -> dict | list:
    # construct full file path
    path = Path(file_path) / file_name

    # check if file exists
    if not path.is_file():
        raise FileNotFoundError(f"file not found at: {path}")

    # read excel file into dataframe
    df = pd.read_excel(path)

    # check if target column exists
    if column_name not in df.columns:
        raise KeyError(
            f"column '{column_name}' not found. available columns: {list(df.columns)}"
        )

    # check if grouping column exists when provided
    if group_column and group_column not in df.columns:
        raise KeyError(
            f"grouping column '{group_column}' not found. available columns: {list(df.columns)}"
        )

    # get unique values by group if specified
    if group_column:
        grouped_results = {}
        print(f"unique values in '{column_name}' grouped by '{group_column}':")
        for group, group_df in df.groupby(group_column):
            unique_vals = group_df[column_name].dropna().unique().tolist()
            grouped_results[group] = unique_vals
            print(f"\nperiod: {group} ({len(unique_vals)} total)")
            for val in unique_vals:
                print(f" - {val}")
        return grouped_results

    # extract unique values excluding missing data
    unique_vals = df[column_name].dropna().unique().tolist()

    print(f"unique values in '{column_name}' ({len(unique_vals)} total):")
    for val in unique_vals:
        print(f" - {val}")

    return unique_vals


if __name__ == "__main__":
    print_unique_column_values(
        file_path=Path("./run_data_malawi/extracts"),
        file_name="lmis_mw_master.xlsx",
        column_name="Product",
        group_column="Period",
    )