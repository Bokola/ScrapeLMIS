# SIMAM (LMIS Mozambique) pipeline

Log into SIMAM -> open Analytics Reports -> Requisition Data Report ->
Download results (xlsx) -> stage raw -> validate schema -> upsert into a
master Excel workbook -> (optional) reconcile against a manual baseline.

This project was adapted from a similar GFPVAN/e2open scraping pipeline.
The infrastructure (config loading, logging, landing-zone staging, schema
validation, upsert-into-master-workbook, reconciliation) carried over
almost unchanged. `scraper.py` is new - SIMAM is a different platform
(OpenLMIS-based) with a completely different login flow, navigation, and
export mechanism.



## Setup

```bash
# 1. Create the venv and install everything from pyproject.toml
uv venv
uv sync

# 2. Install the actual browser binary Playwright drives
uv run playwright install chromium

# 3. Set credentials (never commit real values)
cp .env.example .env
# edit .env with your real LMIS_username / LMIS_pass
```

## Running

```bash
# Full pipeline - .env is picked up automatically
uv run python -m lmis_mz_pipeline.main

# Reconcile against a manually-downloaded baseline file
uv run python -m lmis_mz_pipeline.main --baseline path/to/manual_download.xlsx

# Equivalent, via the installed console script
uv run lmis-mz-pipeline
```

Watch it run instead of headless: set `headless: false` in `config.yaml`.

## Project layout

```
config.yaml                          # everything hot-swappable without code edits
.env.example                         # copy to .env, fill in real credentials
pyproject.toml                       # dependencies + console script entry point
src/lmis_mz_pipeline/
  config.py         Config.load(path) - config.yaml + env-var credentials
  logger.py          shared logging setup
  scraper.py          Playwright driver: login, open report, download results
  landing.py           raw downloaded-file staging
  extract.py            read downloaded file + upsert into master workbook
  schema_validation.py  pandera structural checks
  reconciliation.py      automated vs. manual-baseline comparison
  main.py                 end-to-end orchestrator + CLI
run_data/               gitignored - screenshots, landing zone, extracts, session state
```

## Call graph

```
main.py
 ├─ config.py            Config.load()
 ├─ logger.py             enable_file_logging()
 ├─ scraper.py            open_browser() → LMISScraper:
 │                          ensure_logged_in() → login() → _login_once()
 │                          open_requisition_report() → _wait_for_results_table()
 │                          download_results_xlsx()  (ellipsis icon → Download results → xlsx)
 ├─ landing.py             stage_raw_download(downloaded_path, cfg)
 ├─ extract.py             read_downloaded_file() → pandas DataFrame
 ├─ schema_validation.py   validate_extract(df, schema=LMIS_REQUISITION_SCHEMA)
 ├─ extract.py             upsert_to_excel(df, cfg) → master workbook, deduped on
 │                          Código da instalação + Código do produto + Período de análise
 └─ reconciliation.py      load_baseline(), reconcile(), write_reconciliation_report()
                           (only if --baseline was passed)
```

## Common issues

- **`ImportError: attempted relative import with no known parent package`**
  You ran a file directly instead of through `-m`. See "Why the layout
  matters" above.
- **`ConfigError: Missing required environment variable(s)`**
  `.env` isn't being loaded, or the var names in it don't match
  `credentials.username_env` / `password_env` in `config.yaml`
  (`LMIS_username` / `LMIS_pass` by default).
- **A `first_match()` / `ScraperError` failure on first run**
  Expected the first time. Check `run_data/screenshots/<timestamp>_<tag>.png` and the matching
  `_frameN.html` dumps, then update the relevant selector list in
  `config.yaml`.
- **Playwright browser download fails / blocked host**
  Some sandboxed environments block `cdn.playwright.dev`. Run
  `uv run playwright install chromium` from a machine with normal internet
  access.
