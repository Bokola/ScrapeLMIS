# CLAUDE.md

## Project Guidelines for lmis_pipeline

### Environment & Run Commands
* **Package Manager**: Use `uv` for environment management and execution.
* **Main Pipeline Execution**: `uv run python -m lmis_pipeline.main`
* **Required Environment Variables**:
  * `LMIS_username` (resolved via `credentials.username_env` in config)
  * `LMIS_pass` (resolved via `credentials.password_env` in config)

### Architecture & Configuration
* **Config Driven**: All selectors, URLs, retry policies, and timeouts live in `config.yaml`. Never hardcode selectors or target values in Python scripts.
* **Target Environment**: SIMAM, an OpenLMIS-based portal at `simam.cmam.gov.mz`, hash-routed (`#!/...`).
* **Composite Key**: Master workbook deduplication uses `Código da instalação`, `Código do produto`, `Período de análise`.
* **Data Protection**: The raw downloaded file must be copied into `run_data/landing/` (via `stage_raw_download()`) before schema validation or any downstream transformation occurs.

### Coding Standards & Conventions
* **Imports**: Standard library first, third-party packages second, local module imports third. Always include `from __future__ import annotations`.
* **Comments**: Always keep comments in lowercase, without dots or dashes.
* **Typing**: Enforce strict type annotations across all function arguments and returns (e.g., `list[dict]`, `Path | None`).
* **Error Handling**: Use custom pipeline exception classes (`ConfigError`, `ScraperError`, `AuthenticationError`, `TransientError`, `PipelineError`).
* **Diagnostics**: Do not swallow critical locator failures; ensure diagnostics (HTML/screenshots) are dumped on failure using `dump_diagnostics()`.
* **Selectors**: Always retrieve selectors through `cfg.selectors("section.key")` to leverage resilient fallbacks.

### What still needs confirming against the live site

This project was adapted from a different pipeline (GFPVAN/e2open) without
ever driving the real SIMAM DOM. Before trusting a run, confirm these in
order (run with `headless: false`):

1. **Login form** - CONFIRMED: username is `#login-username`
   (`ng-model="vm.username"`), password is `#login-password`
   (`ng-model="vm.password"`) - a single-step AngularJS form, as assumed.
   `selectors.login.submit_button` and `login_error_banner` are still
   guessed - confirm the real submit button (an AngularJS controller named
   `vm` suggests a `vm.someMethod()` ng-click, worth checking) and what a
   rejected-login screen actually shows. Also still unconfirmed:
   `urls.home` currently doubles as the login-check URL - if SIMAM has a
   separate `/#!/login` route, add a `urls.login` entry and point
   `_login_once()`'s first `goto()` at it.
2. **Requisition Data Report link** - `selectors.nav.requisition_report_link`.
   Only the parent dropdown (`Analytics Reports`) markup is confirmed; the
   report link itself is matched by its visible text as a guess.
3. **Results-table-is-ready signal** - CONFIRMED: the report is a Metabase
   dashboard embedded in an iframe (`prod-metabase.siglus.us`), and its
   table visualization is matched via `[data-testid="table-root"]`, scoped
   to the dashcard whose `data-viz-ui-name="Table"`. `loading_indicator`
   is still a guess, but appears not to matter much in practice - a real
   capture showed the table's data already loaded (`data-rows-count`
   present) well within the existing timeout.
4. **Download flow** - CONFIRMED, all steps, via a real diagnostics
   capture of the actual "Download data" panel: ellipsis icon
   (`[data-testid="public-or-embedded-dashcard-menu"]`) → "Download
   results" menu item → xlsx option in a format SegmentedControl
   (`.mb-mantine-SegmentedControl-innerLabel:text-is('.xlsx')`) → a
   "Download" button (`button[data-testid='download-results-button']`)
   that fires the real download directly. No separate confirm/apply step.
   Two earlier versions of this went down a wrong path (an "Apply" button,
   then an "Apply → reopen menu → click Download results again" dance)
   based on misreading an unrelated "Apply" button whose CSS classes
   didn't match this panel's Mantine styling - it almost certainly
   belonged to the separate product-filter widget instead. The panel
   itself also explicitly warns about large row counts taking a while to
   download, hence `timeouts.download_ms` is generous (180s).
5. **Product filter** (optional, config's `filters.products`) - CONFIRMED,
   fully: it's a Mantine PillsInput + Combobox, not a bulk-paste textarea.
   Each product is selected individually - type its name into
   `report.product_filter_search_input` to filter a dropdown pre-loaded
   with the whole product catalog, click the matching `role="option"`,
   repeat, then click `report.product_filter_apply_button` (starts
   disabled until something's selected). Product names must match SIMAM's
   product master data exactly, punctuation included.

   IMPORTANT, confirmed via a real screenshot: `product_filter_apply_button`
   only STAGES the selection - the filter chip shows "N selections" but
   the report keeps showing its previous, unfiltered data. A separate
   dashboard-level "N filter(s) changed / Cancel / Apply" banner
   (`report.dashboard_apply_button`, `button[aria-label='Apply']`) must
   also be clicked to actually commit the change and re-run the report.
   `set_product_filter()` now does both clicks - skipping the second one
   would silently download unfiltered data even though the filter chip
   itself looks correctly set, which is exactly what happened before this
   was caught.

Each of these, once confirmed, should have its selector list trimmed down
to the real one (keep 1-2 fallbacks, drop the rest) and a short note added
here on what was seen, matching how the original GFPVAN pipeline's CLAUDE.md
documented its own "hard-won" DOM rules.
