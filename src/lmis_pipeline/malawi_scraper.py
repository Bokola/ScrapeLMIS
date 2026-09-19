"""Playwright driver for Malawi's LMIS (lmis.health.gov.mw) - login, navigate
to Reports -> View Reports -> LMIS Summary by facility, fill in the
Generate Report form (Program Name, Period Name, format), and download the
result.

This is a genuinely different site from Mozambique's SIMAM: a native
OpenLMIS "Generate Report" form (select2 dropdown widgets, a plain
<input type=submit> to generate), not a Metabase dashboard embedded in an
iframe. Shares only the browser-lifecycle/diagnostics/selector-matching
infrastructure in scraper_base.py - none of the Mozambique-specific
navigation or download logic applies here.

CONFIRMED (you provided this markup directly): the weak-password warning
modal's Close button, the Reports dropdown, the View Reports link, the
LMIS Summary by facility report link, the rendered (post-selection) state
of the Program Name / Period Name select2 widgets, the xlsx format radio,
and the Generate submit button.

NOT CONFIRMED - genuine guesses, flagged throughout:
  - Malawi's own login form field names/ids. Guessed to match Mozambique's
    confirmed #login-username / #login-password / #login-button, on the
    reasoning that both SIMAM and this site are OpenLMIS deployments (the
    same open-source reference UI, differently configured per country) -
    plausible, but not independently confirmed for this specific site.
  - The exact DOM relationship between a field's <label> and its select2
    widget - the confirmed markup you gave is the widget's own *rendered,
    already-selected* state (id="select2-{{parametername}}-container" is
    literally an unrendered Angular template expression, not a real id),
    not how to find a given field by its label text before anything is
    selected. _select2_choose() below uses a generic XPath heuristic (the
    select2 widget appearing soonest in document order after a <label>
    with the given text) rather than a page-specific confirmed selector.
  - Exactly how "Generate" delivers the file (assumed: a direct browser
    download, same as every other flow in this project so far).

Expect this to need the same iterative, diagnostics-driven fixing that
Mozambique's SIMAM took many rounds to get right - see MALAWI.md.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from tenacity import (
    Retrying,
    retry_if_exception_type,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)

from .config import Config
from .logger import get_logger
from .scraper_base import (
    AUTH_STATUS_CODES,
    TRANSIENT_STATUS_CODES,
    AuthenticationError,
    BaseScraper,
    ScraperError,
    TransientError,
)

log = get_logger(__name__)


class MalawiScraper(BaseScraper):
    """Thin wrapper around a Playwright page bound to Malawi's LMIS."""

    # ---------------------------------------------------------------- session persistence
    def ensure_logged_in(self) -> None:
        assert self.page is not None
        if self.storage_state_path.exists():
            log.info("Verifying saved session is still valid")
            self.page.goto(self.cfg.get("urls.home"))
            self._wait_networkidle()
            if self._looks_logged_in():
                log.info("Saved session is valid - skipping login")
                return
            log.info("Saved session is no longer valid - logging in fresh")

        self.login()

    def _looks_logged_in(self) -> bool:
        assert self.page is not None
        try:
            self.first_match(self.cfg.selectors("nav.logged_in_indicator"), timeout_ms=5000)
            return True
        except ScraperError:
            return False

    def _save_session(self) -> None:
        assert self.context is not None
        self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.context.storage_state(path=str(self.storage_state_path))
        log.info("Saved session state to %s", self.storage_state_path)

    # ---------------------------------------------------------------- login
    def _before_sleep(self, retry_state) -> None:
        wait_s = retry_state.next_action.sleep
        exc = retry_state.outcome.exception()
        log.warning(
            "Login attempt %d failed (%s: %s) - retrying in %.1f minutes",
            retry_state.attempt_number, type(exc).__name__, exc, wait_s / 60,
        )

    def login(self) -> None:
        """Log in to Malawi's LMIS, retrying transient failures with a
        growing, jittered delay. Authentication failures are NEVER
        retried."""
        base_wait_s = self.cfg.get("retry.login.base_wait_seconds", 300)
        backoff_multiplier = self.cfg.get("retry.login.backoff_multiplier", 1.5)
        max_wait_s = self.cfg.get("retry.login.max_wait_seconds", 1800)
        jitter_max_s = self.cfg.get("retry.login.jitter_max_seconds", 45)
        max_attempts = self.cfg.get("retry.login.max_attempts", 4)

        retrying = Retrying(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(
                multiplier=base_wait_s, exp_base=backoff_multiplier, max=max_wait_s
            )
            + wait_random(0, jitter_max_s),
            retry=retry_if_exception_type((TransientError, PWTimeout))
            & retry_if_not_exception_type(AuthenticationError),
            reraise=True,
            before_sleep=self._before_sleep,
        )
        retrying(self._login_once)

    def _login_once(self) -> None:
        assert self.page is not None

        log.info("Navigating to Malawi LMIS")
        response = self.page.goto(self.cfg.get("urls.home"))
        if response is not None:
            status = response.status
            if status in AUTH_STATUS_CODES:
                raise AuthenticationError(f"Login page returned HTTP {status} - check access.")
            if status in TRANSIENT_STATUS_CODES:
                raise TransientError(f"Login page returned HTTP {status}.")
            if status >= 400:
                raise ScraperError(f"Login page returned unexpected HTTP {status}.")

        self._wait_networkidle()

        if self._looks_logged_in():
            log.info("Already logged in")
            self._save_session()
            return

        try:
            log.info("Entering username")
            username_input = self.first_match(
                self.cfg.selectors("login.username_input"), timeout_ms=15000
            )
            username_input.fill(self.cfg.username)

            log.info("Entering password")
            password_input = self.first_match(
                self.cfg.selectors("login.password_input"), timeout_ms=10000
            )
            password_input.fill(self.cfg.password)

            log.info("Submitting login form")
            self._click(self.first_match(
                self.cfg.selectors("login.submit_button"), timeout_ms=10000
            ))
        except ScraperError as e:
            raise TransientError(f"Login form step failed: {e}") from e

        self._dismiss_weak_password_modal()

        try:
            self._wait_networkidle()
            self.first_match(self.cfg.selectors("nav.logged_in_indicator"), timeout_ms=30000)
        except (PWTimeout, ScraperError):
            self.dump_diagnostics("login_redirect_failed")
            for sel in self.cfg.selectors("login.login_error_banner"):
                if self.page.locator(sel).count() > 0:
                    raise AuthenticationError("Login failed - credentials rejected.")
            raise TransientError(
                "Login did not reach a recognizably logged-in page and no error "
                "banner was found. See the 'login_redirect_failed' diagnostics dump."
            )

        log.info("Login successful")
        self._save_session()

    def _dismiss_weak_password_modal(self) -> None:
        """Close the "your password is weak" modal that appears after login
        with a simple/default password. CONFIRMED markup (you provided
        this: <button ng-click="vm.close()">Close</button>). Bounded and
        non-fatal - it may not appear on every login (e.g. once the
        password is eventually changed), so a miss here is not an error."""
        assert self.page is not None
        try:
            close_button = self.first_match(
                self.cfg.selectors("login.weak_password_modal_close_button"),
                timeout_ms=5000,
            )
            log.info("Dismissing weak-password warning modal")
            self._click(close_button)
        except ScraperError:
            log.debug("No weak-password warning modal appeared - continuing")

    # ---------------------------------------------------------------- navigation
    def open_lmis_summary_report(self) -> None:
        """Click Reports -> View Reports -> LMIS Summary by facility,
        landing on the Generate Report options page. CONFIRMED markup for
        all three links."""
        assert self.page is not None

        log.info("Opening Reports dropdown")
        try:
            self._click(self.first_match(
                self.cfg.selectors("nav.reports_dropdown"), timeout_ms=15000
            ))
        except ScraperError as e:
            self.dump_diagnostics("reports_dropdown_not_found")
            raise ScraperError(f"Could not open the Reports dropdown: {e}") from e

        log.info("Clicking View Reports")
        try:
            self._click(self.first_match(
                self.cfg.selectors("nav.view_reports_link"), timeout_ms=10000
            ))
        except ScraperError as e:
            self.dump_diagnostics("view_reports_link_not_found")
            raise ScraperError(f"Could not click View Reports: {e}") from e

        self._wait_networkidle()

        log.info("Selecting LMIS Summary by facility")
        try:
            self._click(self.first_match(
                self.cfg.selectors("nav.lmis_summary_report_link"), timeout_ms=10000
            ))
        except ScraperError as e:
            self.dump_diagnostics("lmis_summary_report_link_not_found")
            raise ScraperError(f"Could not select LMIS Summary by facility: {e}") from e

        self._wait_networkidle()

    # ---------------------------------------------------------------- select2 helper
    def _select2_choose(self, select_id: str, search_text: str) -> None:
        """Open the select2 widget for the underlying <select id="{select_id}">
        and choose the option matching search_text.

        CONFIRMED via a real diagnostics capture that the widget's own id
        (id="select2-{{parametername}}-container" in the raw markup you
        gave) is a genuinely BROKEN, unrendered Angular interpolation on
        this site - not a template artifact from copying the markup, but
        the literal live DOM content for EVERY select2 field on this page,
        so it can't be used to tell one field apart from another. What IS
        confirmed and reliable: each field's underlying (hidden) <select>
        has a real, stable id, taken directly from its own <label
        for="..."> - "program", "period", "district" - and select2 always
        inserts its rendered widget as that <select>'s immediate next
        sibling (the standard select2 DOM pattern). This targets that
        structural relationship rather than the broken id.

        Retries the whole open -> type -> select sequence up to 3 times as
        a defensive measure, though the root cause of the failure this was
        originally added for turned out to be deterministic, not flaky -
        see the comment on the .click() call below.
        """
        assert self.page is not None
        container_selector = f"#{select_id} + span.select2-container span[role='combobox']"
        last_err: Exception | None = None

        for attempt in range(1, 4):
            try:
                container = self.first_match([container_selector], timeout_ms=10000)
                # NOT self._click() here: that helper calls the element's
                # native JS .click() method, which per spec only fires a
                # "click" event - no mousedown/mouseup. Confirmed via a
                # real diagnostics capture that this NEVER actually opened
                # the dropdown here (aria-expanded stayed "false" every
                # single time, zero select2-container--open anywhere) -
                # this select2 widget's opening behavior is bound to a
                # real mouse event sequence (mousedown before click, the
                # classic older-jQuery-select2 pattern), which only
                # Playwright's own .click() simulates.
                container.click()

                # CONFIRMED markup (you provided this): the search input
                # is a plain input.select2-search__field (type="search",
                # role="textbox") - matched directly rather than requiring
                # its parent to carry a "--open" modifier class, in case
                # that assumption doesn't always hold.
                search_input = self.first_match(
                    ["input.select2-search__field", ".select2-container--open .select2-search__field"],
                    timeout_ms=5000,
                )
                # NOT fill(): select2's live search filtering (especially
                # in older jQuery-based versions, likely here) often
                # listens for real keystroke events, not the single bulk
                # "input" event fill() dispatches.
                search_input.press_sequentially(search_text, delay=50)

                option = self.first_match(
                    [f"li.select2-results__option:has-text(\"{search_text}\")"], timeout_ms=10000
                )
                # Same reasoning as the container click above - use
                # Playwright's real .click(), not the native-JS _click().
                option.click()
                return
            except ScraperError as e:
                last_err = e
                log.warning(
                    "select2 selection for #%s (attempt %d/3) failed: %s - retrying",
                    select_id, attempt, e,
                )
                try:
                    self.page.keyboard.press("Escape")
                except Exception:  # noqa: BLE001 - best-effort reset before retrying
                    pass

        self.dump_diagnostics(f"select2_choose_failed_{select_id}")
        raise ScraperError(
            f"Could not select {search_text!r} for #{select_id} after 3 attempts: {last_err}"
        )

    # ---------------------------------------------------------------- generate + download
    def generate_report(
        self, program: str, period: str, download_dir: str | Path
    ) -> Path:
        """Fill in Program Name, Period Name (District left blank), select
        xlsx format, click Generate, and save the resulting file into
        download_dir. Returns the saved path.

        CONFIRMED markup: the xlsx format radio and the Generate submit
        button. NOT CONFIRMED: exactly how Generate delivers the file -
        assumed to be a direct browser download, same as every other flow
        in this project; if it instead opens a new tab/page or requires a
        further click on a "download when ready" link, this will need
        adjusting once a real run shows what actually happens.
        """
        assert self.page is not None
        download_dir = Path(download_dir)
        download_dir.mkdir(parents=True, exist_ok=True)

        log.info("Selecting Program Name: %s", program)
        self._select2_choose(
            self.cfg.get("filters.program_field_id", "program"), program
        )

        log.info("Selecting Period Name: %s", period)
        self._select2_choose(
            self.cfg.get("filters.period_field_id", "period"), period
        )

        # District is deliberately left blank/untouched - per explicit
        # instruction, not a gap to fill in later.

        log.info("Selecting xlsx format")
        try:
            self._click(self.first_match(
                self.cfg.selectors("report.format_radio_xlsx"), timeout_ms=10000
            ))
        except ScraperError as e:
            self.dump_diagnostics("format_radio_xlsx_not_found")
            raise ScraperError(f"Could not select the xlsx format radio: {e}") from e

        log.info("Clicking Generate and waiting for the file")
        try:
            with self.page.expect_download(
                timeout=self.cfg.get("timeouts.download_ms", 180000)
            ) as download_info:
                self._click(self.first_match(
                    self.cfg.selectors("report.generate_button"), timeout_ms=10000
                ))
            download = download_info.value
        except (ScraperError, PWTimeout) as e:
            self.dump_diagnostics("generate_download_failed")
            raise ScraperError(f"Could not trigger the report download: {e}") from e

        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        suggested = download.suggested_filename or f"lmis_mw_summary_{period}_{ts}.xlsx"
        dest = download_dir / f"{ts}_{suggested}"
        download.save_as(str(dest))
        log.info("Saved downloaded results to %s", dest)
        return dest


def open_browser(cfg: Config) -> MalawiScraper:
    """Convenience factory matching `with open_browser(cfg) as s:` usage."""
    return MalawiScraper(cfg)
