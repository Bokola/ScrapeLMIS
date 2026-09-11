# SKILL: confirming SIMAM's real DOM against this pipeline

This pipeline was built without ever driving the live SIMAM site (no
credentials, no network access to `simam.cmam.gov.mz` from the environment
that wrote it). `config.yaml`'s selectors are a mix of a few CONFIRMED
markup snippets you provided directly and a larger set of best-effort
guesses, in the same "ordered list, first match wins" style as the pipeline
this was adapted from. Use this checklist the first time you run it.

## Before your first run

1. Set `headless: false` in `config.yaml` so you can watch the browser.
2. `cp .env.example .env` and fill in real `LMIS_username` / `LMIS_pass`.
3. `uv run python -m lmis_pipeline.main`

## What to watch for, in order

### 1. Login
Watch what happens when the browser lands on `https://simam.cmam.gov.mz/#!/home`
while logged out.
- If it redirects to a distinct login URL (e.g. `#!/login`), add it as
  `urls.login` in config and point `_login_once()`'s first `page.goto()`
  call at that instead of `urls.home`.
- Confirm the real field names/ids for username and password (right-click →
  Inspect), and the real submit button text (likely Portuguese - "Entrar"
  is guessed). Trim `selectors.login.username_input` /
  `password_input` / `submit_button` down to what's real.
- Confirm what a failed login actually looks like (an error banner's real
  text/class) and update `selectors.login.login_error_banner`.

### 2. Post-login "am I logged in" check
`selectors.nav.logged_in_indicator` currently reuses the Analytics Reports
dropdown itself. Confirm this element is reliably present right after
login (not, say, hidden behind a "select your facility" step first) -
if there's an intermediate step, both `_looks_logged_in()` and
`open_requisition_report()` need to account for it.

### 3. Requisition Data Report link
Confirm the real markup for the "Requisition Data Report" item inside the
Analytics Reports dropdown - is it an `<a>`, an `<li>`, does it carry an
`ng-click`? Trim `selectors.nav.requisition_report_link`.

### 4. Results table readiness
Since the report "runs immediately" with no filter step, the pipeline needs
to know when the (possibly 37,000+ row) results table has actually
finished rendering, not just that the page navigated. Watch for a
spinner/loading state and confirm its real markup for
`selectors.report.loading_indicator`, and confirm the results table itself
is reliably matched by `selectors.report.results_table` once ready.

### 5. Download results → xlsx
The ellipsis icon and "Download results" menu item are confirmed. Click
through it once and note the exact label for the xlsx option (vs. csv) -
update `selectors.report.xlsx_format_option` to match exactly, keeping
only 1-2 real fallbacks.

## If a selector fails

Every `first_match()` failure calls `dump_diagnostics()` first, which
writes:
- `run_data/screenshots/<timestamp>_<tag>.png` - what the page looked like
- `run_data/screenshots/<timestamp>_<tag>_frame<N>.html` - the HTML of
  every frame on the page at that moment (in case the report viewer turns
  out to live inside an iframe)

Read the relevant HTML dump, find the real selector, and update the
matching list in `config.yaml` - no code changes needed for a pure
selector fix.

## Once everything above is confirmed

Update `CLAUDE.md`'s "What still needs confirming against the live site"
section with what you found (real selectors, any extra steps like an
intermediate facility picker), the same way the original GFPVAN pipeline's
CLAUDE.md accumulated its own "Hard-Won Autocomplete Rules" over time.
