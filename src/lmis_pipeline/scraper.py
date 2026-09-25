"""Playwright driver: login, navigate to the Analytics Reports dropdown, open
the Requisition Data Report, and download its results.

Selector resolution: each logical selector in config.yaml is a list of
fallbacks. first_match() (in scraper_base.py, shared across countries)
iterates them and returns the first that resolves to a visible locator.
This keeps the script alive across minor DOM changes.

Anti-detection: browser context is aligned to a real desktop UA, pt-MZ
locale, and Africa/Maputo timezone; playwright-stealth patches common
automation signatures; a saved session (storageState) is reused across runs
via ensure_logged_in() so a fresh login only happens when the saved session
has actually expired.

Requires: pip install playwright-stealth  (or: uv add playwright-stealth)

This module is Mozambique/SIMAM-specific. Shared browser lifecycle,
diagnostics, and selector-matching infrastructure lives in scraper_base.py
(BaseScraper) - split out when Malawi (malawi_scraper.py) was added, so
neither country's fixes risk breaking the other's.

NOTE: this module only drives the browser through login -> open the report
-> trigger the "Download results" -> xlsx flow, and hands back the path
Playwright saved the download to. It does not read/validate/write Excel -
see extract.py for that.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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


class LMISScraper(BaseScraper):
    """Thin wrapper around a Playwright page bound to SIMAM (Mozambique)."""

    # ---------------------------------------------------------------- session persistence
    def ensure_logged_in(self) -> None:
        """Reuse a saved session if it's still valid; otherwise perform a
        full login and persist the resulting session for next run."""
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
            self.first_match(
                self.cfg.selectors("nav.logged_in_indicator"),
                timeout_ms=5000,
            )
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
        """Log in to SIMAM, retrying transient failures with a growing,
        jittered delay. Authentication failures (bad credentials, 401/403)
        are NEVER retried."""
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
        """A single login attempt. Raises AuthenticationError for
        credential/authorization failures (not retried) and TransientError
        for anything plausibly worth retrying.

        NOT CONFIRMED: assumes visiting urls.home while unauthenticated
        either shows or redirects to a single-step username+password form
        (typical OpenLMIS behavior) rather than SIMAM having a separate
        login URL or a multi-step flow. If this turns out wrong, the fix is
        almost certainly just adding a urls.login entry and pointing this
        method's first goto() at it.
        """
        assert self.page is not None

        log.info("Navigating to SIMAM")
        response = self.page.goto(self.cfg.get("urls.home"))
        if response is not None:
            status = response.status
            if status in AUTH_STATUS_CODES:
                raise AuthenticationError(f"Home page returned HTTP {status} - check access.")
            if status in TRANSIENT_STATUS_CODES:
                raise TransientError(f"Home page returned HTTP {status}.")
            if status >= 400:
                raise ScraperError(f"Home page returned unexpected HTTP {status}.")

        self._wait_networkidle()

        # Already logged in via a reused session that survived the goto -
        # no login form to fill.
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
            # force=True: a stale, aria-hidden="true" loading-modal from an
            # earlier state persistently occupies screen space in this app
            # (confirmed via a real diagnostics capture) and never actually
            # resolves to Playwright's "hidden" state, so waiting for it to
            # clear before clicking (tried in an earlier version of this
            # method) just burns the full timeout every time for nothing.
            # aria-hidden="true" is the page's own signal that this overlay
            # is inactive, so skipping Playwright's "is anything covering
            # this click?" check here is safe, not reckless.
            self._click(self.first_match(
                self.cfg.selectors("login.submit_button"), timeout_ms=10000
            ))
        except ScraperError as e:
            raise TransientError(f"Login form step failed: {e}") from e

        # NOTE: no wait for the loading-modal here. It was originally
        # assumed to be a transient spinner tied to the login POST, but a
        # real diagnostics capture proved otherwise: this element is
        # aria-hidden="true" and never actually resolves to Playwright's
        # "hidden" state at all - waiting for it just burns the full
        # timeout on every single login for no benefit. Detecting whether
        # login succeeded is handled entirely by the logged_in_indicator
        # check below, which polls for the real success signal directly
        # and isn't affected by this stale element either way.
        success_pattern = self.cfg.get("urls.login_success_pattern")
        try:
            if success_pattern:
                self.page.wait_for_url(success_pattern, timeout=30000)
            self._wait_networkidle()
            self.first_match(
                self.cfg.selectors("nav.logged_in_indicator"), timeout_ms=30000
            )
        except (PWTimeout, ScraperError):
            self.dump_diagnostics("login_redirect_failed")
            for sel in self.cfg.selectors("login.login_error_banner"):
                if self.page.locator(sel).count() > 0:
                    raise AuthenticationError("Login failed - credentials rejected.")
            raise TransientError(
                "Login did not reach a recognizably logged-in page and no error "
                "banner was found - possible slow redirect or unexpected screen. "
                "See the 'login_redirect_failed' diagnostics dump."
            )

        log.info("Login successful")
        self._save_session()

    # ---------------------------------------------------------------- language
    def set_language_english(self) -> None:
        """Switch SIMAM's UI language to English via the header's language
        dropdown, if it isn't already. Idempotent and non-fatal - if the
        dropdown or the English option can't be found, this logs a warning
        and lets the pipeline continue rather than aborting a run over a
        cosmetic step (see config.yaml's ui.force_english to disable
        entirely).
        """
        assert self.page is not None
        if not self.cfg.get("ui.force_english", True):
            return

        try:
            button = self.first_match(
                self.cfg.selectors("common.language_dropdown_button"), timeout_ms=10000
            )
        except ScraperError as e:
            log.warning("Language dropdown not found - leaving UI language as-is: %s", e)
            return

        if "english" in button.inner_text().strip().lower():
            log.info("UI language is already English")
            return

        log.info("Switching UI language to English")
        try:
            self._click(button)
            self._click(self.first_match(
                self.cfg.selectors("common.language_option_english"), timeout_ms=10000
            ))
            self._wait_networkidle()
            log.info("Switched UI language to English")
        except ScraperError as e:
            self.dump_diagnostics("language_switch_failed")
            log.warning(
                "Could not switch UI language to English - continuing in "
                "whatever language is currently shown: %s", e,
            )

    # ---------------------------------------------------------------- navigation
    def open_requisition_report(self) -> None:
        """Click the Analytics Reports dropdown, then Requisition Data
        Report, and wait for the results table to actually render (not just
        the page to navigate) - CONFIRMED behavior per your description:
        the report runs immediately with no filter step.
        """
        assert self.page is not None
        log.info("Opening Analytics Reports dropdown")
        try:
            self._click(self.first_match(
                self.cfg.selectors("nav.analytics_reports_dropdown"),
                timeout_ms=self.cfg.get("timeouts.report_run_ms", 45000),
            ))
        except ScraperError as e:
            self.dump_diagnostics("analytics_reports_dropdown_not_found")
            raise ScraperError(
                "Could not open Analytics Reports dropdown - login itself "
                "succeeded (we got past that check), so this is either a "
                "slow-loading nav menu (try again, or raise "
                "timeouts.report_run_ms) or this account genuinely doesn't "
                "have the role/right to see the Analytics Reports menu item "
                "(check the user's permissions in SIMAM/OpenLMIS admin). "
                f"Original error: {e}"
            ) from e

        log.info("Selecting Requisition Data Report")
        try:
            self._click(self.first_match(
                self.cfg.selectors("nav.requisition_report_link"), timeout_ms=10000
            ))
        except ScraperError as e:
            self.dump_diagnostics("requisition_report_link_not_found")
            raise ScraperError(f"Could not select Requisition Data Report: {e}") from e

        self._wait_networkidle()
        self._wait_for_results_table()

    def _wait_for_results_table(self) -> None:
        """Wait for the report's results table to be visible, tolerating a
        loading spinner/state first if one appears. NOT CONFIRMED: exact
        loading-indicator markup - see config.yaml's report.loading_indicator."""
        assert self.page is not None
        timeout_ms = self.cfg.get("timeouts.report_run_ms", 45000)

        loading_selectors = self.cfg.selectors("report.loading_indicator")
        for sel in loading_selectors:
            try:
                loc = self.page.locator(sel).first
                if loc.count() > 0:
                    log.info("Waiting for loading indicator to clear: %s", sel)
                    loc.wait_for(state="hidden", timeout=timeout_ms)
                    break
            except PWTimeout:
                log.debug("Loading indicator %s did not clear in time - continuing anyway", sel)

        try:
            self.first_match(self.cfg.selectors("report.results_table"), timeout_ms=timeout_ms)
            log.info("Requisition report results table is visible")
        except ScraperError as e:
            self.dump_diagnostics("results_table_not_found")
            raise ScraperError(f"Requisition report results table never appeared: {e}") from e

    # ---------------------------------------------------------------- filtering
    def set_period_filter(self, months: int | None) -> None:
        """Restrict the Requisition Data Report's "Período de análise"
        filter to the previous N months. A no-op if months is falsy/None -
        SIMAM's own default period (confirmed: previous 3 months) is left
        as-is.

        CONFIRMED markup for the widget's label/trigger, its "Previous"
        tab (already the default selection - clicked anyway for
        robustness rather than assuming it stays the default forever),
        the numeric interval input, and an "Update filter" button (all
        via markup you provided). NOT SEPARATELY CONFIRMED for this
        specific widget, but reasoned by direct analogy with
        set_product_filter(): that per-widget confirm button almost
        certainly only STAGES the change, same as the product filter's
        own "Add filter" did - so the dashboard-level Apply banner
        (report.dashboard_apply_button) is clicked afterward too, on the
        assumption this is a dashboard-wide mechanism rather than
        something specific to the product filter.
        """
        assert self.page is not None
        if not months:
            log.info("No period filter configured - leaving the report's default period as-is")
            return

        log.info("Opening the 'Período de análise' filter widget")
        try:
            self._click(self.first_match(
                self.cfg.selectors("report.period_filter_widget"), timeout_ms=15000
            ))
        except ScraperError as e:
            self.dump_diagnostics("period_filter_widget_not_found")
            raise ScraperError(f"Could not open the period filter widget: {e}") from e

        log.info("Selecting the 'Previous' tab")
        try:
            previous_tab = self.first_match(
                self.cfg.selectors("report.period_filter_previous_tab"), timeout_ms=10000
            )
            if previous_tab.get_attribute("aria-selected") == "true":
                log.info("'Previous' tab is already selected - skipping the click")
            else:
                self._click(previous_tab)
        except ScraperError as e:
            self.dump_diagnostics("period_filter_previous_tab_not_found")
            raise ScraperError(f"Could not select the 'Previous' tab: {e}") from e

        log.info("Setting the interval to %d month(s)", months)
        try:
            interval_input = self.first_match(
                self.cfg.selectors("report.period_filter_interval_input"), timeout_ms=10000
            )
            interval_input.fill(str(months))
        except ScraperError as e:
            self.dump_diagnostics("period_filter_interval_input_not_found")
            raise ScraperError(
                f"Could not find the period filter's interval input: {e}"
            ) from e

        log.info("Updating the period filter")
        try:
            self._click(self.first_match(
                self.cfg.selectors("report.period_filter_update_button"), timeout_ms=10000
            ))
        except ScraperError as e:
            self.dump_diagnostics("period_filter_update_button_not_found")
            raise ScraperError(f"Could not click Update filter: {e}") from e

        log.info("Clicking the dashboard-level Apply banner to commit the period change")
        try:
            self._click(self.first_match(
                self.cfg.selectors("report.dashboard_apply_button"), timeout_ms=10000
            ))
        except ScraperError as e:
            self.dump_diagnostics("dashboard_apply_button_not_found")
            raise ScraperError(
                f"Could not find the dashboard-level Apply banner: {e}"
            ) from e

        self._wait_networkidle()
        self._wait_for_results_table()

    def set_product_filter(self, products: list[str]) -> None:
        """Restrict the Requisition Data Report to specific products via its
        "Nome do produto" dashboard filter widget, then wait for the
        (re-filtered) results table again. A no-op if products is empty -
        the report is left showing everything, same as before this method
        existed.

        CONFIRMED markup for the widget's own label/trigger, and, via a
        real diagnostics capture, that it's a Mantine PillsInput + Combobox
        widget, not a plain textarea - there is no bulk-paste entry. It's a
        search-as-you-type field backed by a dropdown pre-loaded with the
        entire product catalog (role="option" divs), and each product must
        be selected individually: type its name to filter the dropdown,
        click the matching option (adds it as a removable "pill"), clear
        the search text, and repeat for the next product. Only once every
        product is selected is the widget's own "Add filter" button
        (initially disabled) clickable.

        ALSO CONFIRMED, via a real screenshot: confirming that per-widget
        button only stages the selection - the filter chip shows "N
        selections" but the report keeps showing its previous, unfiltered
        data - so a separate dashboard-level "Apply" banner
        (report.dashboard_apply_button) must be clicked too, to actually
        commit the change and re-run the report. Skipping that second
        click would otherwise silently download unfiltered data even
        though the filter chip itself looks correctly set.
        """
        assert self.page is not None
        if not products:
            log.info("No product filter configured - leaving the report unfiltered")
            return

        log.info("Opening the 'Nome do produto' filter widget")
        try:
            self._click(self.first_match(
                self.cfg.selectors("report.product_filter_widget"), timeout_ms=15000
            ))
        except ScraperError as e:
            self.dump_diagnostics("product_filter_widget_not_found")
            raise ScraperError(f"Could not open the product filter widget: {e}") from e

        log.info("Selecting %d product(s) from the filter's search list", len(products))
        try:
            search_input = self.first_match(
                self.cfg.selectors("report.product_filter_search_input"), timeout_ms=10000
            )
        except ScraperError as e:
            self.dump_diagnostics("product_filter_search_input_not_found")
            raise ScraperError(
                f"Could not find the product filter's search input: {e}"
            ) from e

        for product in products:
            try:
                # Search on a shorter slice rather than the full string -
                # confirmed via a real product-master data extract that
                # this exact product name is correct and exists verbatim in
                # SIMAM's system, yet the live search-as-you-type widget
                # returned "No matching Productname found" for it. The
                # product name is also the only one in this list containing
                # a "/" or "." (in its dosage, e.g. "104mg/0.65ml") -
                # plausibly breaking whatever query the widget builds
                # internally. Searching on the portion before the first
                # semicolon (typically just the drug/brand name) avoids
                # those characters entirely, and is still specific enough
                # not to collide with this pipeline's other product names.
                # The actual option clicked is still matched against the
                # FULL product string, so this can't select the wrong item.
                search_term = product.split(";")[0].strip()
                search_input.fill(search_term)
                option = self.first_match(
                    [f'[role="option"]:has-text("{product}")'], timeout_ms=10000
                )
                self._click(option)
                search_input.fill("")
            except ScraperError as e:
                self.dump_diagnostics("product_filter_option_not_found")
                raise ScraperError(
                    f"Could not find/select the option for product "
                    f"{product!r} in the filter's search list - the exact "
                    f"text must match SIMAM's own product master data "
                    f"(punctuation included): {e}"
                ) from e

        log.info("Applying the product filter")
        try:
            self._click(self.first_match(
                self.cfg.selectors("report.product_filter_apply_button"), timeout_ms=10000
            ))
        except ScraperError as e:
            self.dump_diagnostics("product_filter_apply_button_not_found")
            raise ScraperError(f"Could not find the filter's apply button: {e}") from e

        # CONFIRMED via a real screenshot: the widget's own "Add filter"
        # button only stages the selection (the filter chip shows "N
        # selections" but the report keeps showing its previous,
        # unfiltered data) - a separate dashboard-level "N filter(s)
        # changed / Cancel / Apply" banner has to be clicked too, to
        # actually commit the change and re-run the report against it.
        log.info("Clicking the dashboard-level Apply banner to commit the filter change")
        try:
            self._click(self.first_match(
                self.cfg.selectors("report.dashboard_apply_button"), timeout_ms=10000
            ))
        except ScraperError as e:
            self.dump_diagnostics("dashboard_apply_button_not_found")
            raise ScraperError(
                f"Could not find the dashboard-level Apply banner: {e}"
            ) from e

        self._wait_networkidle()
        self._wait_for_results_table()

        if self.cfg.get("filters.verify_applied", True):
            applied_count = self._count_applied_filter_values("nome_do_produto")
            if applied_count != len(products):
                self.dump_diagnostics("product_filter_verification_failed")
                raise ScraperError(
                    f"Product filter verification failed: requested "
                    f"{len(products)} product(s) but the report's own "
                    f"embedded dashboard URL shows {applied_count} actually "
                    f"applied. Refusing to proceed to download, since that "
                    f"would otherwise silently pull the ENTIRE unfiltered "
                    f"report with no warning. Set filters.verify_applied: "
                    f"false in config.yaml to disable this check if it's "
                    f"producing false positives."
                )
            log.info("Verified %d product filter(s) actually applied", applied_count)

    def set_program_filter(self, program: str | None) -> None:
        """Restrict the Requisition Data Report to a specific Programa
        (e.g. "TARV" for HIV/ART data) via the dashboard's "Programa"
        filter widget, then wait for the (re-filtered) results table
        again. A no-op if program is falsy/None - the report's default
        program scope (everything) is left as-is.

        CONFIRMED via a real diagnostics capture: the "Programa" widget's
        own label/trigger opens successfully by direct analogy with "Nome
        do produto"'s pattern, but it is otherwise a DIFFERENT widget type,
        not the same Mantine PillsInput+Combobox - it's a checkbox list.
        Each option is a real <input type="checkbox"
        data-testid="{value}-filter-value">, not a role="option" div (an
        earlier version of this method assumed the latter and never found
        a match). The search input happens to share the same placeholder
        text as the product widget's, but has its own distinct
        data-testid - see report.program_filter_search_input. The apply
        button is confirmed to be the same aria-label="Add filter" button
        used by the product filter.

        Verification (filters.verify_applied) assumes the resulting
        dashboard URL uses a "programa" query parameter, by analogy with
        the confirmed "nome_do_produto" - not independently confirmed.
        """
        assert self.page is not None
        if not program:
            log.info("No program filter configured - leaving the report's default program scope as-is")
            return

        log.info("Opening the 'Programa' filter widget")
        try:
            self._click(self.first_match(
                self.cfg.selectors("report.program_filter_widget"), timeout_ms=15000
            ))
        except ScraperError as e:
            self.dump_diagnostics("program_filter_widget_not_found")
            raise ScraperError(f"Could not open the program filter widget: {e}") from e

        log.info("Selecting program: %s", program)
        try:
            search_input = self.first_match(
                self.cfg.selectors("report.program_filter_search_input"), timeout_ms=10000
            )
            search_input.fill(program)
            # checkbox list, not role="option" divs - see docstring above.
            # CONFIRMED via a real diagnostics capture that this checkbox
            # click can silently fail to register (no "checked" attribute
            # afterward, matching widget's own "Add filter" button stayed
            # disabled) - verify-and-retry rather than trust a single
            # click, same defensive pattern used for Malawi's select2
            # flakiness.
            checkbox_selector = f'input[data-testid="{program}-filter-value"]'
            checkbox = self.first_match(
                [checkbox_selector, f'label:has-text("{program}")'], timeout_ms=10000
            )
            checked = False
            for attempt in range(1, 4):
                self._click(checkbox)
                try:
                    cb = self.first_match([checkbox_selector], timeout_ms=2000)
                    if cb.is_checked():
                        checked = True
                        break
                except ScraperError:
                    pass
                log.warning(
                    "Checkbox for program %r did not register as checked "
                    "(attempt %d/3) - retrying", program, attempt,
                )
            if not checked:
                raise ScraperError(
                    f"Checkbox for program {program!r} never registered as "
                    f"checked after 3 attempts"
                )
        except ScraperError as e:
            self.dump_diagnostics("program_filter_option_not_found")
            raise ScraperError(
                f"Could not find/select the program option {program!r}: {e}"
            ) from e

        log.info("Applying the program filter")
        try:
            self._click(self.first_match(
                self.cfg.selectors("report.product_filter_apply_button"), timeout_ms=10000
            ))
        except ScraperError as e:
            self.dump_diagnostics("program_filter_apply_button_not_found")
            raise ScraperError(f"Could not find the program filter's apply button: {e}") from e

        log.info("Clicking the dashboard-level Apply banner to commit the program filter change")
        try:
            self._click(self.first_match(
                self.cfg.selectors("report.dashboard_apply_button"), timeout_ms=10000
            ))
        except ScraperError as e:
            self.dump_diagnostics("dashboard_apply_button_not_found")
            raise ScraperError(f"Could not find the dashboard-level Apply banner: {e}") from e

        self._wait_networkidle()
        self._wait_for_results_table()

        if self.cfg.get("filters.verify_applied", True):
            applied_count = self._count_applied_filter_values("programa")
            if applied_count < 1:
                self.dump_diagnostics("program_filter_verification_failed")
                raise ScraperError(
                    f"Program filter verification failed: requested "
                    f"program {program!r} but the report's own embedded "
                    f"dashboard URL shows no 'programa' value applied "
                    f"(this parameter name is a guess, not confirmed - if "
                    f"the URL uses a different name, this check will "
                    f"always fail; check the diagnostics dump and either "
                    f"fix the param name here or set "
                    f"filters.verify_applied: false to disable this check)."
                )
            log.info("Verified program filter actually applied")

    def _count_applied_filter_values(self, url_param_name: str) -> int:
        """Count how many values for the given Metabase dashboard URL query
        parameter are actually present in the embedded iframe's own URL -
        this is ground truth for what's actually filtered (confirmed
        present in every successful run's diagnostics so far, for
        "nome_do_produto"), independent of whether our own click sequence
        happened to raise an error or not. Used as a safety net against a
        filter click sequence that completes without error but didn't
        actually register any selection.
        """
        assert self.page is not None
        for frame in self.page.frames:
            if url_param_name in frame.url or "metabase" in frame.url.lower():
                parsed = urlparse(frame.url)
                qs = parse_qs(parsed.query)
                return len(qs.get(url_param_name, []))
        return 0

    # ---------------------------------------------------------------- download
    def download_results_xlsx(self, download_dir: str | Path) -> Path:
        """Open the results menu (ellipsis icon) -> Download results ->
        select xlsx format -> Download, and save the resulting file into
        download_dir. Returns the saved path.

        CONFIRMED, all steps, via a real diagnostics capture of the actual
        "Download data" panel: the ellipsis icon, "Download results" menu
        item, xlsx option in a format SegmentedControl, and a "Download"
        button (data-testid="download-results-button") that fires the
        real download directly - no separate confirm/apply step, contrary
        to an earlier version of this method. That earlier version
        inserted an extra "Apply -> reopen menu -> Download results again"
        detour based on a misread "Apply" button whose CSS classes
        (emotion-prefixed) don't match this panel's Mantine styling at all
        - it almost certainly belonged to the separate product-filter
        widget, not this download panel. The panel itself also warns
        "Your answer has a large number of rows so it could take a while
        to download" - hence the generous download_ms timeout below.
        """
        assert self.page is not None
        download_dir = Path(download_dir)
        download_dir.mkdir(parents=True, exist_ok=True)

        log.info("Opening results menu (ellipsis icon)")
        try:
            self._click(self.first_match(
                self.cfg.selectors("report.ellipsis_menu_button"), timeout_ms=15000
            ))
        except ScraperError as e:
            self.dump_diagnostics("ellipsis_menu_not_found")
            raise ScraperError(f"Could not open the results menu: {e}") from e

        log.info("Clicking Download results")
        try:
            self._click(self.first_match(
                self.cfg.selectors("report.download_results_item"), timeout_ms=10000
            ))
        except ScraperError as e:
            self.dump_diagnostics("download_results_item_not_found")
            raise ScraperError(f"Could not find 'Download results': {e}") from e

        log.info("Selecting xlsx format")
        try:
            self._click(self.first_match(
                self.cfg.selectors("report.xlsx_format_option"), timeout_ms=10000
            ))
        except ScraperError as e:
            self.dump_diagnostics("xlsx_format_option_not_found")
            raise ScraperError(f"Could not select the xlsx format option: {e}") from e

        log.info("Clicking Download and waiting for the file")
        try:
            with self.page.expect_download(
                timeout=self.cfg.get("timeouts.download_ms", 180000)
            ) as download_info:
                self._click(self.first_match(
                    self.cfg.selectors("report.download_confirm_button"), timeout_ms=10000
                ))
            download = download_info.value
        except (ScraperError, PWTimeout) as e:
            self.dump_diagnostics("xlsx_download_failed")
            raise ScraperError(f"Could not trigger the download: {e}") from e

        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        suggested = download.suggested_filename or f"lmis_mz_requisition_{ts}.xlsx"
        dest = download_dir / f"{ts}_{suggested}"
        download.save_as(str(dest))
        log.info("Saved downloaded results to %s", dest)
        return dest


def open_browser(cfg: Config) -> LMISScraper:
    """Convenience factory matching `with open_browser(cfg) as s:` usage."""
    return LMISScraper(cfg)
