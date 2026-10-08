# NIGERIA: forward load, family planning

A pipeline driven against `https://healthlmis.ng/`. The whole flow has now
completed on a real run: login, sync, the Analytics page, the export, landing
the file. That run took 19 minutes (sync 4, page load 10, export 3).

## Before your first run

1. `cp .env.example .env` and fill in `LMIS_NG_username` and `LMIS_NG_pass`
   (leave the other countries' lines as they are).
2. `config_nigeria.yaml` already has `headless: false`, so you can watch it.
3. `uv run python -m lmis_pipeline.nigeria_main`

## What it does

Works out the latest **completed** two month window, logs in, waits for the
data sync that follows login, opens the Analytics page (the Analytics folder
in the sidebar, then Analytics in its dropdown), sets the window with the
**Prev / Next buttons of the time filter** (the label between them, like
`May-Jun 2026`, is read after every press), then clicks
**`LMIS & Service Data Export`** in the bar fixed to the bottom of the page and
lands the downloaded xlsx untouched, then adds its `Family Planning` sheet to
the master extract (see below). The landed file itself is never edited.

Three buttons sit in that bottom bar: `Indicator Export` (a csv of the current
indicator, which is what was downloaded by mistake before and is never clicked
now), `LMIS & Service Data Export` and `LMD Order/Stock Export`. The exports
follow the time filter at the top, there is no window control of their own.

Waits are real, since every run starts with a clean browser with no local data.
On the real runs the sync took about 215 seconds and the Analytics page then
took 557 seconds before its controls appeared, and the page says the export
itself takes several minutes, so **expect a run to take 15 to 30 minutes**.
Progress is logged every 30 seconds for each wait. Limits:
`timeouts.sync_ms` (10 minutes, carries on with a warning),
`timeouts.page_load_ms` (20 minutes), `timeouts.window_change_ms` (2 minutes per
press) and `timeouts.download_ms` (15 minutes).

| Run in | Window downloaded | Landed as |
|---|---|---|
| Sep or Oct 2026 | Jul-Aug 2026 | `LMIS_NG_2026_07_to_08.xlsx` |
| Nov or Dec 2026 | Sep-Oct 2026 | `LMIS_NG_2026_09_to_10.xlsx` |
| Jan or Feb 2027 | Nov-Dec 2026 | `LMIS_NG_2026_11_to_12.xlsx` |

The window containing today is still open, so it is skipped. Files go to
`run_data_nigeria/landing/`; a rerun for the same window replaces the file.
Every raw download is also kept, timestamped and never overwritten, in
`run_data_nigeria/downloads/`.

Two checks run on the download before anything is landed. It must be an
`.xlsx` (`export.expected_extension`) and its first sheet must be named
`Family Planning` (`export.expected_first_sheet`). If either fails the run
stops, nothing is landed, and any earlier landed file for that window is left
alone. The raw download is always kept in `run_data_nigeria/downloads/`.

## The extract

After a file lands, its `Family Planning` sheet is written to
`run_data_nigeria/extracts/` under **the same file name as the landing file**,
for example `LMIS_NG_2026_07_to_08.xlsx` in both `landing/` and `extracts/`.

- The landing file is the whole export, untouched. The extract has only the
  `Family Planning` sheet, written as it is: no added columns, no merging with
  other windows.
- Cells keep the type they have in the workbook, so a code stored as text keeps
  its leading zero and a number stays a number.
- A rerun of a window replaces both files. A refused export (wrong extension or
  sheet) never touches either.

To write the extracts for files that were landed before this existed, with no
browser and no waiting:

```
uv run python -m lmis_pipeline.nigeria_main --extract-only
```

## Historical load

Set the range in `config_nigeria.yaml` and run with `--historical` (or set
`historical.enabled: true`):

```yaml
historical:
  enabled: false
  start: "Jan-Feb 2025"   # a window label, written like the site shows it
  end: "auto"             # the latest completed window, or a label to stop earlier
  skip_existing: true
```

```
uv run python -m lmis_pipeline.nigeria_main --historical
```

- It logs in, syncs and opens the Analytics page **once**, then does each
  window newest first (the page opens on the latest window, so that is the
  fewest Prev presses). Each window is downloaded, checked, landed and
  extracted exactly like a normal run, under the same file names.
- Every window costs about 3 minutes of export plus the Prev press, on top of
  the 15 minutes of login, sync and page load that are paid once. Twelve
  windows (two years) is roughly 1 hour 15 minutes.
- A window that fails is logged and the next one is tried. After
  `export.max_consecutive_failures` (2) failures in a row it stops, since the
  session is probably in a bad state. The end of the run lists what failed and
  what was not attempted, and the exit code is 1.
- **Run it again to continue.** A window that already has both its landing and
  its extract file is skipped (`skip_existing`), so an interrupted or partly
  failed backfill picks up where it stopped. Set `skip_existing: false` to
  fetch everything again.
- `end` must be a completed window and not before `start`, otherwise it stops
  before opening the browser.
- Windows the site has no data for may not produce a download. That window then
  waits out `timeouts.download_ms` (15 minutes) and counts as a failure.

## Browser messages in the log

The site produces a lot of messages by itself. They were all in the first full
run's log and none of them stopped anything:

| Message | What it is |
|---|---|
| `404` on `/db/<name>/_local/...` | the app looking up its sync checkpoint, which does not exist yet in a fresh browser. Appears at the start and end of each database sync |
| `404` on `...amazonaws.com/stock-report-export/...xlsx` | the page polling every 30 seconds for the export file while the server builds it. The download started right after the last one |
| `console error: Failed to load resource ... 404` | the browser repeating the line above it |
| `SYNC_ERROR, source: alerts ... reading 'filter'` | an error in the site's own code, the pipeline never uses alerts |
| `POST .../_changes (net::ERR_ABORTED)` | the app cancelling a request |

These cannot be fixed from here, so they are recognised (`browser_noise` in the
config) and counted in one line at the end, like `Ignored 38 expected browser
message(s) ...`, instead of being logged as warnings. **Anything else the
browser reports is still a warning.** A new kind of message that is harmless
can be added to `browser_noise` with a name and a regex.

## What's confirmed vs. guessed

**Confirmed** by a real run: login, sync, the route to the page, the time
filter and the bottom bar, that `LMIS & Service Data Export` downloads an xlsx
with `Family Planning` as its first sheet, and that it takes about 3 minutes.

**Not exercised yet**: pressing Prev or Next. The page already showed the right
window on that run, so no press was needed. It is tested against a mock only.
If the page ever opens on another window, the label is read after every press
and a press that changes nothing fails with a dump.

## If a step fails

Each failure saves a screenshot and the rendered html of every frame to
`run_data_nigeria/screenshots/` under one of these tags. Send the log plus the
`_frame0.html` for that tag.

| Tag | Step that failed |
|---|---|
| `ng_login_form_not_found` | username or password field not found |
| `ng_login_not_confirmed` | submitted, but the logged in page never appeared |
| `ng_export_page_not_found` | the Analytics folder or its Analytics dropdown item not found (the dump has the folder open, so it lists its real items) |
| `ng_export_page_not_ready` | reached the Analytics page but neither the time filter nor the export buttons appeared. Either still loading after `timeouts.page_load_ms`, or finished loading with neither. The log says which |
| `ng_window_button_not_found` | could not read the window label |
| `ng_window_step_not_found` | could not find Prev or Next |
| `ng_window_not_applied` | pressed Prev or Next but the label did not change, or the target was not reached in `export.max_window_steps` presses |
| `ng_export_button_not_found` | no `LMIS & Service Data Export` button, even after scrolling down |
| `ng_export_download_failed` | clicked it, but no file arrived within `timeouts.download_ms`. Send the dump, it shows any dialog |

While a page is slow, the log also lists what the browser reported, as
`Browser reported N problem(s)` lines: failed requests, HTTP errors (400 and
up) and console errors. Query strings are removed, so no tokens are logged.
That is how a page that is stuck is told apart from one that is only slow.

The login page load is retried once after a short wait (`retry.login`), but
credentials are only ever submitted once, to avoid a lockout.
