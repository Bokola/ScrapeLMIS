"""Playwright driver: login, navigate to the Analytics Reports dropdown, open
the Requisition Data Report, and download its results.

Selector resolution: each logical selector in config.yaml is a list of
fallbacks. first_match() iterates them and returns the first that resolves
to a visible locator. This keeps the script alive across minor DOM changes.

Anti-detection: browser context is aligned to a real desktop UA, pt-MZ
locale, and Africa/Maputo timezone; playwright-stealth patches common
automation signatures; a saved session (storageState) is reused across runs
via ensure_logged_in() so a fresh login only happens when the saved session
has actually expired.

Requires: pip install playwright-stealth  (or: uv add playwright-stealth)

IMPORTANT - carried over from the pipeline this was adapted from, but doubly
true here: SIMAM (an OpenLMIS-based portal, different from the GFPVAN/e2open
site this scraper started life against) has NOT been driven live by this
code yet. Login field names, the exact report-run/loading behavior, and the
xlsx-format menu option are all best-effort guesses - see config.yaml's
selectors section and SKILL.md for what's confirmed vs. guessed, and run
with headless: false the first time so you can watch it and fix whichever
selector misses.

NOTE: this module only drives the browser through login -> open the report
-> trigger the "Download results" -> xlsx flow, and hands back the path
Playwright saved the download to. It does not read/validate/write Excel -
see extract.py for that.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Frame,
    Locator,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
    sync_playwright,
)
from playwright_stealth import Stealth
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

log = get_logger(__name__)

AUTH_STATUS_CODES = {401, 403}
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class ScraperError(RuntimeError):
    pass


class AuthenticationError(ScraperError):
    """Credentials were rejected, or the server returned a 401/403. Never
    retried - retrying won't fix bad credentials and risks a lockout."""


class TransientError(ScraperError):
    """A retryable failure: timeout, unresolved selector (page likely still
    loading), or a 429/5xx from the server."""


class LMISScraper:
    """Thin wrapper around a Playwright page bound to SIMAM."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.pw: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.shots_dir = cfg.root / "screenshots"
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self.storage_state_path = Path(
            cfg.get("browser.storage_state_path", "./run_data/storage_state.json")
        )
        self._frame_cache: dict[tuple[str, ...], Frame] = {}

    # ---------------------------------------------------------------- lifecycle
    def __enter__(self) -> "LMISScraper":
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(headless=self.cfg.headless)

        context_kwargs: dict = dict(
            viewport={"width": 1600, "height": 900},
            accept_downloads=True,
            user_agent=self.cfg.get("browser.user_agent", DEFAULT_USER_AGENT),
            locale=self.cfg.get("browser.locale", "pt-MZ"),
            timezone_id=self.cfg.get("browser.timezone", "Africa/Maputo"),
        )

        if self.storage_state_path.exists():
            log.info("Found saved session state at %s", self.storage_state_path)
            context_kwargs["storage_state"] = str(self.storage_state_path)
        else:
            log.info("No saved session state found - will need a fresh login")

        self.context = self.browser.new_context(**context_kwargs)

        if self.cfg.get("browser.stealth", True):
            Stealth().apply_stealth_sync(self.context)
            log.info("Applied playwright-stealth evasions to browser context")

        self.page = self.context.new_page()
        self.page.set_default_timeout(self.cfg.get("timeouts.action_ms", 30000))
        self.page.set_default_navigation_timeout(self.cfg.get("timeouts.navigation_ms", 60000))
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            self.snapshot("uncaught_exception")
        try:
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
            if self.pw:
                self.pw.stop()
        except Exception as e:  # noqa: BLE001 - best effort cleanup
            log.warning("Error during teardown: %s", e)

    # ---------------------------------------------------------------- utilities
    def snapshot(self, tag: str) -> Path:
        """Save full page screenshot for debugging. Returns path."""
        if not self.page:
            return Path()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.shots_dir / f"{ts}_{tag}.png"
        try:
            self.page.screenshot(path=str(path), full_page=True)
            log.info("Screenshot saved: %s", path.name)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not capture screenshot: %s", e)
        return path

    def _wait_networkidle(self, timeout_ms: int = 15000) -> None:
        """wait_for_load_state("networkidle"), bounded and non-fatal - an
        Angular/React SPA can keep polling in the background and prevent
        networkidle from ever firing. Every wait_for_load_state("networkidle")
        call in this class should go through this method, not call
        page.wait_for_load_state directly (see CLAUDE.md's rationale in the
        pipeline this was adapted from - the same failure mode is plausible
        here)."""
        assert self.page is not None
        try:
            self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PWTimeout:
            log.debug(
                "networkidle wait timed out after %dms - continuing anyway", timeout_ms
            )

    def _wait_for_loading_overlay_clear(self, timeout_ms: int | None = None) -> None:
        """Wait for OpenLMIS's global loading-spinner modal to disappear, if
        one is currently showing. Bounded and non-fatal.

        NOT CURRENTLY CALLED ANYWHERE - kept for a genuinely transient
        spinner if one turns up elsewhere, but confirmed via a real
        diagnostics capture that the login page's own `.loading-modal` is
        NOT one: it's marked aria-hidden="true" but never actually
        satisfies Playwright's "hidden" state, so every call to this method
        against that element burned its full timeout for nothing (this
        produced a real, reproducible ~1-minute-per-call stall in an
        earlier version of the login flow). That element is instead
        handled by clicking through it with force=True - see
        _login_once()."""
        assert self.page is not None
        timeout = timeout_ms or self.cfg.get("timeouts.action_ms", 30000)
        for sel in self.cfg.selectors("common.loading_overlay"):
            try:
                loc = self.page.locator(sel).first
                if loc.count() > 0:
                    loc.wait_for(state="hidden", timeout=timeout)
            except PWTimeout:
                log.debug(
                    "Loading overlay %s did not clear within %dms - continuing anyway",
                    sel, timeout,
                )

    def _click(self, locator: Locator) -> None:
        """Click an element via its own native DOM .click() method rather
        than a simulated mouse event at its screen coordinates.

        This app has a persistent, aria-hidden="true" overlay that
        genuinely occupies screen space in front of some elements
        (confirmed via a real diagnostics capture, and the likely cause of
        credentials being entered but the submit click having no visible
        effect). Playwright's click(force=True) skips its OWN pre-check for
        "is something covering this?", but still dispatches a real mouse
        event at the element's screen coordinates - if something really is
        on top at that exact point, the browser's native hit-testing can
        still deliver the click to the overlay instead of the intended
        element, with no error raised either way.

        Calling the element's own .click() via evaluate() bypasses
        coordinate-based hit-testing entirely, so it always fires on the
        intended node regardless of what's visually on top of it. This
        still works for both AngularJS's ng-click (listens for real click
        events) and React/Mantine's delegated event system (also listens
        for real click events bubbling up to the document), since both
        respond to a genuine dispatched click event no matter how it was
        triggered."""
        locator.evaluate("el => el.click()")

    def dump_diagnostics(self, tag: str) -> None:
        """On a selector failure: save a screenshot and dump the HTML of
        every frame (main page + any iframes), plus log every frame URL
        present. Not yet confirmed whether SIMAM's report viewer lives in
        an iframe or the main document - dumping every frame costs nothing
        and covers either case."""
        if not self.page:
            return
        self.snapshot(tag)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        frames = list(self.page.frames)
        for i, frame in enumerate(frames):
            try:
                html_path = self.shots_dir / f"{ts}_{tag}_frame{i}.html"
                html_path.write_text(frame.content(), encoding="utf-8")
                log.info("Saved frame %d HTML (url=%s) to %s", i, frame.url, html_path)
            except Exception as e:  # noqa: BLE001
                log.warning("Could not save HTML for frame %d (url=%s): %s", i, frame.url, e)

        try:
            frame_urls = [f.url for f in frames]
            log.info("Frames present on page (%d total): %s", len(frame_urls), frame_urls)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not enumerate frames: %s", e)

    def first_match(
        self,
        candidates: list[str],
        timeout_ms: int | None = None,
        search_frames: bool = True,
    ) -> Locator:
        """Return the first locator from candidates that becomes visible,
        checking EVERY frame (main page + every iframe) on every poll cycle,
        not one frame/candidate at a time with its own full timeout each.

        This matters a lot on this site specifically: SIMAM's Requisition
        Data Report is a Metabase dashboard embedded in an iframe, so
        selectors for it only ever match inside that iframe, never the main
        page. Checking the main frame first with a full timeout per
        candidate before ever trying the iframe (the previous approach)
        could burn minutes finding nothing, purely because of frame check
        order - not because anything was actually slow. Polling all frames
        together each cycle returns the instant a match appears anywhere,
        regardless of which frame it's in.
        """
        assert self.page is not None
        timeout_s = (timeout_ms or self.cfg.get("timeouts.short_ms", 5000)) / 1000
        poll_interval_s = 0.25
        cache_key = tuple(candidates)
        last_err: Exception | None = None
        deadline = monotonic() + timeout_s

        while True:
            frames = [self.page.main_frame]
            if search_frames:
                frames += [f for f in self.page.frames if f != self.page.main_frame]
            # try the frame that matched last time first - cheap optimization,
            # not required for correctness since every frame is checked anyway
            cached_frame = self._frame_cache.get(cache_key)
            if cached_frame in frames:
                frames.remove(cached_frame)
                frames.insert(0, cached_frame)

            for frame in frames:
                for sel in candidates:
                    try:
                        loc = frame.locator(sel).first
                        if loc.count() > 0 and loc.is_visible():
                            self._frame_cache[cache_key] = frame
                            if frame != self.page.main_frame:
                                log.debug("Selector matched inside iframe (%s): %s", frame.url, sel)
                            return loc
                    except Exception as e:  # noqa: BLE001 - frame may have navigated/detached mid-check
                        last_err = e
                        continue

            if monotonic() >= deadline:
                break
            sleep(poll_interval_s)

        raise ScraperError(
            f"None of the selectors matched in any frame within {timeout_s:.1f}s: "
            f"{candidates} (last error: {last_err})"
        )

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
            applied_count = self._count_applied_product_filters()
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

    def _count_applied_product_filters(self) -> int:
        """Count how many nome_do_produto values are actually present in the
        embedded Metabase dashboard's own iframe URL - this is ground truth
        for what's actually filtered (confirmed present in every successful
        run's diagnostics so far), independent of whether our own click
        sequence happened to raise an error or not. Used as a safety net
        against a filter click sequence that completes without error but
        didn't actually register any selections."""
        assert self.page is not None
        for frame in self.page.frames:
            if "nome_do_produto" in frame.url or "metabase" in frame.url.lower():
                parsed = urlparse(frame.url)
                qs = parse_qs(parsed.query)
                return len(qs.get("nome_do_produto", []))
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
