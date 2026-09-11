# MALAWI: confirming the real DOM against this pipeline

This is a much newer, much less-confirmed pipeline than the Mozambique one
(`config.yaml` / `scraper.py`). Expect this to need the same kind of
iterative, diagnostics-driven fixing that Mozambique took many rounds to
get right - that's normal, not a sign anything is badly wrong.

## Before your first run

1. `config_malawi.yaml` already has `headless: false`.
2. `cp .env.example .env` and fill in real `LMIS_MW_username` / `LMIS_MW_pass`
   (leave the Mozambique `LMIS_username` / `LMIS_pass` lines as-is too, if
   you're using both pipelines from the same checkout).
3. `uv run python -m lmis_pipeline.malawi_main`

## What's confirmed vs. guessed

**Confirmed** (you provided this markup directly):
- The weak-password warning modal's Close button
- The Reports dropdown, View Reports link, and LMIS Summary by facility
  report link
- The Program Name / Period Name select2 widgets' *rendered, already-
  selected* appearance (not how to find them before selecting anything)
- The xlsx format radio and Generate submit button

**Guessed, in priority order of what's most likely to need fixing first:**

1. **Login form fields.** `selectors.login.username_input` /
   `password_input` / `submit_button` are copied from Mozambique's
   confirmed SIMAM values (`#login-username` etc.), on the reasoning that
   both are OpenLMIS deployments sharing the same reference UI. Plausible,
   not verified for this specific site. If login fails, inspect the real
   login page's username/password/submit elements the same way you did
   for SIMAM (right-click → Inspect → Copy outerHTML) and update
   `config_malawi.yaml`.

2. ~~Finding a select2 widget by its field label~~ - **CONFIRMED**, fixed.
   The literal id `select2-{{parametername}}-container` turned out to be a
   genuinely broken, unrendered Angular interpolation on this site (not a
   template artifact from copying markup - it's the real live DOM content
   for every select2 field, identical across all of them, so it can never
   distinguish one from another). `_select2_choose()` now instead uses
   each field's real, stable underlying `<select id="program"/"period"/
   "district">` (taken from its own `<label for="...">`) plus select2's
   standard sibling-DOM pattern: `#program + span.select2-container
   span[role='combobox']`. A first version of this method guessed at a
   label-text-proximity heuristic instead and never actually opened the
   dropdown at all (confirmed via a real diagnostics capture: zero
   select2-search/dropdown markup appeared anywhere on the page
   afterward).

3. **How Generate actually delivers the file.** Assumed to be a direct
   browser download, same as every other flow in this project. If
   `generate_report()` times out waiting for a "download" event, check
   (with `headless: false`) whether a new tab/window opens instead, or
   whether the report is generated asynchronously with a separate
   "download when ready" link appearing later - either would need a
   different mechanism than `page.expect_download()`.

4. **The post-login "am I logged in" signal.** Currently reuses the
   Reports dropdown itself (`nav.logged_in_indicator`). This exact pattern
   caused real problems for Mozambique early on (a race between login
   succeeding and the nav menu finishing rendering) - if Malawi shows the
   same symptom, the fix there was to use something true immediately on
   login instead (a logout button/link, if one exists here).

## If a selector fails

Same as Mozambique: every `first_match()` failure calls
`dump_diagnostics()`, which writes a screenshot plus the HTML of every
frame to `run_data_malawi/screenshots/`. Read the relevant HTML dump, find
the real selector or structure, and update `config_malawi.yaml` (or, for
the select2 traversal logic specifically, `malawi_scraper.py`'s
`_select2_choose()`).

## Schema

There is no confirmed column schema for the "LMIS Summary by facility"
report yet - `config_malawi.yaml`'s `excel_columns` is empty and
`validation.fail_on_error` is `false`, so nothing is enforced until you've
seen a real export. Once you have one, list its actual columns in
`excel_columns` and consider building a `MALAWI_SCHEMA` in
`schema_validation.py`, the same way `GFPVAN_EXTRACT_SCHEMA` and
`LMIS_REQUISITION_SCHEMA` were each built from a real sample file.

## Once everything above is confirmed

Update this file's "confirmed vs. guessed" section with what you found,
the same way `CLAUDE.md` accumulated Mozambique's own hard-won rules over
many rounds of fixes.
