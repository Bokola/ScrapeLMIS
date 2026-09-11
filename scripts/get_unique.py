# /// script
# dependencies = [
#   "pandas",
#   "openpyxl",
# ]
# ///

from pathlib import Path
import pandas as pd


def print_unique_column_values(filename: str, column_name: str) -> list:
    # construct path to file inside target directory
    file_path = Path("./run_data/extracts") / filename

    # check if file exists
    if not file_path.is_file():
        raise FileNotFoundError(f"file not found at: {file_path}")

    # read excel file into dataframe
    df = pd.read_excel(file_path)

    # check if column exists
    if column_name not in df.columns:
        raise KeyError(
            f"column '{column_name}' not found. available columns: {list(df.columns)}"
        )

    # extract unique values excluding missing data
    unique_vals = df[column_name].dropna().unique().tolist()

    # print unique values
    print(f"unique values in '{column_name}' ({len(unique_vals)} total):")
    for val in unique_vals:
        print(f" - {val}")

    return unique_vals


if __name__ == "__main__":
    # replace 'sample.xlsx' and 'your_column_name' with your actual file and column
    print_unique_column_values("lmis_mz_master.xlsx", "Nome do produto")