"""Playwright driver for Nigeria's LMIS (healthlmis.ng): log in, wait out the
data sync, open the Analytics page, set the two month window with its Prev and
Next buttons and click LMIS & Service Data Export in the bar at the bottom.

Confirmed from a real dump of the loaded page: the time filter (Prev, a label
like May-Jun 2026, Next), and the footer bar with Indicator Export (a csv of the
current indicator, never used), LMIS & Service Data Export and LMD Order/Stock
Export. The exports follow the time filter. Also the login, the sync, the route
to the Analytics page and that it takes minutes to load.

Not confirmed: whether LMIS & Service Data Export downloads straight away or
asks something first, and how long it takes. A failure at any step dumps a
screenshot and the rendered html of every frame under a tag starting with ng_,
see NIGERIA.md.
"""
from __future__ import annotations

import re
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Self
from urllib.parse import urlsplit

from playwright.sync_api import (
    ConsoleMessage,
    Download,
    Locator,
    Page,
    Request,
    Response,
)
from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
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

# seconds between "still syncing" log lines, so a long wait never looks like a hang
SYNC_LOG_EVERY_S = 30.0
# how long to wait for the sync indicator to show up before assuming there is no sync
SYNC_APPEAR_S = 5.0
# seconds between "still loading" log lines on the analytics page, also the length of
# each wait slice while it loads
PAGE_LOG_EVERY_S = 30.0
# how long the page may be done loading, with no window selector, before giving up
LOADED_GRACE_S = 20.0


def _safe_url(url: str) -> str:
    """Scheme, host and path only, so query strings (which can carry tokens)
    never reach the log."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"[:200]


def _strip_query(text: str) -> str:
    return re.sub(r"(https?://[^\s?]+)\?\S*", r"\1", text)[:300]


def _normalize(text: str) -> str:
    """Collapse any run of whitespace, including non breaking spaces."""
    return " ".join(text.split())


MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_WINDOW_RE = re.compile(r"([A-Za-z]{3})-([A-Za-z]{3}) (\d{4})")


def window_index(label: str) -> int:
    """Position of a window label like May-Jun 2026 on a line of two month windows."""
    m = _WINDOW_RE.fullmatch(label.strip())
    if not m or m.group(1).title() not in MONTH_ABBR:
        raise ScraperError(f"{label!r} is not a window label like May-Jun 2026")
    return int(m.group(3)) * 6 + MONTH_ABBR.index(m.group(1).title()) // 2


def window_label(index: int) -> str:
    year, k = divmod(index, 6)
    return f"{MONTH_ABBR[2 * k]}-{MONTH_ABBR[2 * k + 1]} {year}"


class NigeriaScraper(BaseScraper):
    """Playwright page bound to Nigeria's LMIS."""

    def __enter__(self) -> Self:
        super().__enter__()
        assert self.page is not None
        # what the browser reports while a page sits on loading, kept so a stuck
        # page can be told apart from a slow one in the log
        self._problems: deque[str] = deque(maxlen=200)
        # messages the site produces by itself (see browser_noise in the config) are
        # counted instead of warned about
        self._noise = [
            (item["name"], re.compile(item["pattern"])) for item in self.cfg.get("browser_noise", [])
        ]
        self._noise_counts: Counter[str] = Counter()
        self.page.on("console", self._on_console)
        self.page.on("requestfailed", self._on_request_failed)
        self.page.on("response", self._on_response)
        return self

    def _on_console(self, message: ConsoleMessage) -> None:
        if message.type == "error":
            self._problems.append(f"console error: {_strip_query(message.text)}")

    def _on_request_failed(self, request: Request) -> None:
        self._problems.append(
            f"request failed: {request.method} {_safe_url(request.url)} ({request.failure})"
        )

    def _on_response(self, response: Response) -> None:
        if response.status >= 400:
            self._problems.append(
                f"HTTP {response.status}: {response.request.method} {_safe_url(response.url)}"
            )

    def _expected_name(self, line: str) -> str | None:
        for name, pattern in self._noise:
            if pattern.search(line):
                return name
        return None

    def _log_browser_problems(self) -> None:
        """Warn about what the browser reported, apart from the messages the
        site is known to produce by itself, which are only counted."""
        lines = list(self._problems)
        self._problems.clear()
        unexpected: list[str] = []
        for line in lines:
            name = self._expected_name(line)
            if name:
                self._noise_counts[name] += 1
            elif line not in unexpected:
                unexpected.append(line)
        if unexpected:
            log.warning("Browser reported %d unexpected problem(s) since the last check", len(unexpected))
            for line in unexpected[-15:]:
                log.warning("  %s", line)

    def _log_noise_summary(self) -> None:
        if self._noise_counts:
            parts = ", ".join(f"{name} x{count}" for name, count in self._noise_counts.most_common())
            log.info(
                "Ignored %d expected browser message(s) the site produces by itself: %s",
                sum(self._noise_counts.values()), parts,
            )

    def __exit__(self, *exc: object) -> bool | None:
        try:
            self._log_browser_problems()
            self._log_noise_summary()
        finally:
            return super().__exit__(*exc)  # noqa: B012

    # ---------------------------------------------------------------- clicking
    def _real_click(self, locator: Locator) -> None:
        """Click with a real mouse event (mousedown, mouseup, click).

        The native el.click() used for SIMAM only fires a click event, and
        Malawi showed that some widgets bind to mousedown instead, so a real
        click is the safer default for a site nobody has driven yet. Any
        failure becomes a ScraperError so callers can dump diagnostics.
        """
        try:
            locator.click(timeout=self.cfg.get("timeouts.action_ms", 30000))
        except (PWTimeout, PWError) as e:
            raise ScraperError(f"could not click element: {e}") from e

    # ---------------------------------------------------------------- login
    def _before_sleep(self, retry_state: RetryCallState) -> None:
        wait_s = retry_state.next_action.sleep if retry_state.next_action else 0.0
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        log.warning(
            "Page load attempt %d failed (%s: %s), retrying in %.0f seconds",
            retry_state.attempt_number, type(exc).__name__, exc, wait_s,
        )

    def login(self) -> None:
        """Open the landing page, retrying transient load failures, then
        submit credentials once. The credential submit is never retried, a
        wrong guess repeated risks a lockout."""
        retrying = Retrying(
            stop=stop_after_attempt(self.cfg.get("retry.login.max_attempts", 2)),
            wait=wait_exponential(
                multiplier=self.cfg.get("retry.login.base_wait_seconds", 20),
                exp_base=self.cfg.get("retry.login.backoff_multiplier", 1.5),
                max=self.cfg.get("retry.login.max_wait_seconds", 120),
            )
            + wait_random(0, self.cfg.get("retry.login.jitter_max_seconds", 5)),
            retry=retry_if_exception_type(TransientError),
            reraise=True,
            before_sleep=self._before_sleep,
        )
        retrying(self._open_landing_page)
        self._submit_credentials()

    def _open_landing_page(self) -> None:
        assert self.page is not None
        log.info("Navigating to Nigeria LMIS")
        try:
            response = self.page.goto(self.cfg.get("urls.home"))
        except (PWTimeout, PWError) as e:
            raise TransientError(f"landing page did not load: {e}") from e
        if response is None:
            return
        status = response.status
        if status in AUTH_STATUS_CODES:
            raise AuthenticationError(f"Landing page returned HTTP {status}, check access.")
        if status in TRANSIENT_STATUS_CODES:
            raise TransientError(f"Landing page returned HTTP {status}.")
        if status >= 400:
            raise ScraperError(f"Landing page returned unexpected HTTP {status}.")

    def _submit_credentials(self) -> None:
        assert self.page is not None
        if not self.cfg.username or not self.cfg.password:
            raise ScraperError("credentials are not loaded, check the .env file")

        # the form is drawn by javascript, so the first wait is the long one
        app_ready_ms = self.cfg.get("timeouts.app_ready_ms", 60000)
        try:
            log.info("Entering username")
            self.first_match(
                self.cfg.selectors("login.username_input"), timeout_ms=app_ready_ms
            ).fill(self.cfg.username)

            log.info("Entering password")
            password_input = self.first_match(
                self.cfg.selectors("login.password_input"), timeout_ms=10000
            )
            password_input.fill(self.cfg.password)
        except ScraperError as e:
            self.dump_diagnostics("ng_login_form_not_found")
            raise ScraperError(f"could not find the login form fields: {e}") from e

        log.info("Submitting login form")
        try:
            self._real_click(
                self.first_match(self.cfg.selectors("login.submit_button"), timeout_ms=5000)
            )
        except ScraperError as e:
            # enter in the password field submits most forms, and the
            # logged in check below catches it if that does not work either
            log.warning("No submit button clicked (%s), pressing Enter instead", e)
            password_input.press("Enter")

        try:
            self.first_match(self.cfg.selectors("app.logged_in"), timeout_ms=app_ready_ms)
        except ScraperError as e:
            self.dump_diagnostics("ng_login_not_confirmed")
            raise ScraperError(
                "the logged in page never appeared after submitting credentials, "
                "so the login is not confirmed (rejected credentials, or a different "
                f"page after login): {e}"
            ) from e
        log.info("Login confirmed")

    # ---------------------------------------------------------------- sync and navigation
    def _any_present(self, selectors: list[str]) -> bool:
        assert self.page is not None
        for selector in selectors:
            try:
                if self.page.locator(selector).count() > 0:
                    return True
            except PWError as e:
                log.debug("presence check failed for %s: %s", selector, e)
        return False

    def _syncing(self) -> bool:
        return self._any_present(self.cfg.selectors("app.sync_in_progress"))

    def wait_for_sync(self) -> None:
        """Wait for the data sync that follows login. The app keeps showing
        Loading in its main area until it finishes, and a fresh browser has
        no local data, so it can take minutes. Not fatal on a timeout, the
        element waits in the next steps are what really decide."""
        assert self.page is not None
        limit_s = self.cfg.get("timeouts.sync_ms", 600000) / 1000
        start = monotonic()
        next_log_s = SYNC_LOG_EVERY_S
        seen = False
        while True:
            elapsed = monotonic() - start
            if self._syncing():
                seen = True
            elif seen:
                log.info("Sync finished after %.0f seconds", elapsed)
                return
            elif elapsed >= SYNC_APPEAR_S:
                log.info("No sync in progress")
                return
            if elapsed >= limit_s:
                log.warning(
                    "Still syncing after %.0f seconds, carrying on anyway "
                    "(raise timeouts.sync_ms if it is just slow)", elapsed,
                )
                return
            if seen and elapsed >= next_log_s:
                log.info("Still syncing after %.0f seconds", elapsed)
                self._log_browser_problems()
                next_log_s += SYNC_LOG_EVERY_S
            self.page.wait_for_timeout(500)

    def open_export_page(self) -> None:
        """Open the Analytics page from the sidebar: click the Analytics
        folder, then pick Analytics from the dropdown it opens. The folder is
        only clicked when the dropdown item is not already showing, because
        clicking an open folder closes it."""
        item_selectors = self.cfg.selectors("app.analytics_item")
        try:
            try:
                item = self.first_match(item_selectors, timeout_ms=3000)
            except ScraperError:
                log.info("Opening the Analytics menu")
                self._real_click(
                    self.first_match(self.cfg.selectors("app.analytics_menu"), timeout_ms=10000)
                )
                item = self.first_match(item_selectors, timeout_ms=15000)
            log.info("Selecting Analytics from the dropdown")
            self._real_click(item)
        except ScraperError as e:
            # the dump is taken with the analytics menu open, so it lists its real items
            self.dump_diagnostics("ng_export_page_not_found")
            raise ScraperError(f"could not open the Analytics page from the sidebar menu: {e}") from e

        try:
            self.wait_for_page_ready()
        except ScraperError as e:
            # the dump shows the analytics page, which is where the next step lives
            self._log_browser_problems()
            self.dump_diagnostics("ng_export_page_not_ready")
            raise ScraperError(f"opened the Analytics page but it never became usable: {e}") from e
        log.info("Analytics page is open")

    def wait_for_page_ready(self) -> None:
        """Wait until the analytics page shows a window selector or the export
        section. The page shows Loading for a while after it opens (9 minutes
        on the first real run) and only draws its controls once that is done,
        so this waits in slices and says how it is going. It gives up early
        when the page has finished loading and shows neither, since waiting
        longer would not help."""
        selectors = self.cfg.selectors("app.window_button") + self.cfg.selectors("app.export_button")
        limit_s = self.cfg.get("timeouts.page_load_ms", 1200000) / 1000
        start = monotonic()
        loaded_since: float | None = None
        while True:
            try:
                self.first_match(selectors, timeout_ms=int(PAGE_LOG_EVERY_S * 1000))
                return
            except ScraperError as e:
                elapsed = monotonic() - start
                loading = self._any_present(self.cfg.selectors("app.page_loading"))
                if loading:
                    loaded_since = None
                elif loaded_since is None:
                    loaded_since = monotonic()
                log.info(
                    "Analytics page not ready after %.0f seconds (%s)",
                    elapsed, "still loading" if loading else "finished loading",
                )
                self._log_browser_problems()
                if loaded_since is not None and monotonic() - loaded_since >= LOADED_GRACE_S:
                    raise ScraperError(
                        "the page finished loading but shows neither the window selector nor "
                        "the export buttons, so they are not where they were expected"
                    ) from e
                if elapsed >= limit_s:
                    raise ScraperError(
                        f"the page was still loading after {limit_s:.0f} seconds "
                        "(raise timeouts.page_load_ms if it is just slow)"
                    ) from e

    # ---------------------------------------------------------------- window
    def _window_button(self, timeout_ms: int | None = None) -> Locator:
        return self.first_match(
            self.cfg.selectors("app.window_button"),
            timeout_ms=timeout_ms or self.cfg.get("timeouts.app_ready_ms", 60000),
        )

    def read_window_label(self) -> str:
        """Text of the window button, which shows the window the page, and so
        its exports, are set to."""
        return _normalize(self._window_button().inner_text())

    def select_window(self, label: str) -> None:
        """Make label (for example Jul-Aug 2026) the selected window by
        pressing Prev or Next on the time filter, one window at a time, and
        reading the label after every press. The exports at the bottom of the
        page follow this filter."""
        assert self.page is not None
        target = window_index(label)
        max_steps = self.cfg.get("export.max_window_steps", 12)
        wait_s = self.cfg.get("timeouts.window_change_ms", 120000) / 1000
        for _ in range(max_steps + 1):
            try:
                current = self.read_window_label()
                position = window_index(current)
            except ScraperError as e:
                self.dump_diagnostics("ng_window_button_not_found")
                raise ScraperError(f"could not read the selected window: {e}") from e
            if position == target:
                log.info("Window %s is selected", label)
                return
            direction = "next" if target > position else "prev"
            log.info("Window shows %s, pressing %s to reach %s", current, direction, label)
            try:
                self._real_click(
                    self.first_match(self.cfg.selectors(f"app.window_{direction}"), timeout_ms=10000)
                )
            except ScraperError as e:
                self.dump_diagnostics("ng_window_step_not_found")
                raise ScraperError(f"could not press {direction} on the time filter: {e}") from e
            deadline = monotonic() + wait_s
            changed = False
            while monotonic() < deadline:
                self.page.wait_for_timeout(500)
                try:
                    changed = self.read_window_label() != current
                except ScraperError:
                    continue
                if changed:
                    break
            if not changed:
                self.dump_diagnostics("ng_window_not_applied")
                raise ScraperError(
                    f"pressed {direction} but the window still shows {current!r} after "
                    f"{wait_s:.0f} seconds"
                )
        self.dump_diagnostics("ng_window_not_applied")
        raise ScraperError(f"did not reach {label!r} after {max_steps} presses")

    # ---------------------------------------------------------------- download
    def _find_export_button(self) -> Locator:
        """Find the LMIS & Service Data Export button at the bottom of the
        page and scroll it into view, scrolling down in steps if it is not
        there yet."""
        assert self.page is not None
        selectors = self.cfg.selectors("app.export_button")
        steps = self.cfg.get("export.scroll_steps", 30)
        step_px = self.cfg.get("export.scroll_step_px", 900)
        for _ in range(steps + 1):
            try:
                button: Locator | None = self.first_match(selectors, timeout_ms=2000)
            except ScraperError:
                button = None
            if button is not None:
                try:
                    button.scroll_into_view_if_needed(timeout=10000)
                except (PWTimeout, PWError) as e:
                    raise ScraperError(f"found the export button but could not scroll to it: {e}") from e
                return button
            # the page scrolls inside itself, so the wheel goes over the middle of the content
            self.page.mouse.move(900, 500)
            self.page.mouse.wheel(0, step_px)
            self.page.wait_for_timeout(700)
        raise ScraperError(f"the export button did not appear after scrolling down {steps} times")

    def download_export(self, download_dir: str | Path) -> Path:
        """Click LMIS & Service Data Export and save the file it downloads
        into download_dir, untouched. Returns the saved path. The site says
        this is a very large export that takes several minutes."""
        assert self.page is not None
        assert self.context is not None
        download_dir = Path(download_dir)
        download_dir.mkdir(parents=True, exist_ok=True)

        try:
            button = self._find_export_button()
        except ScraperError as e:
            self.dump_diagnostics("ng_export_button_not_found")
            raise ScraperError(f"could not find the LMIS & Service Data Export button: {e}") from e

        try:
            log.info("Exporting with the window showing %s", self.read_window_label())
        except ScraperError:
            log.warning("Could not read the window before exporting")
        log.info(
            "Clicking LMIS & Service Data Export and waiting for the file. The server builds it and "
            "the page polls for it, so 404s from the site's export storage are expected until it is ready"
        )

        downloads: list[Download] = []

        def _on_download(download: Download) -> None:
            downloads.append(download)

        def _on_new_page(popup: Page) -> None:
            # a link that opens a new tab reports its download on that tab
            popup.on("download", _on_download)

        # listeners instead of expect_download, so a failed click raises at
        # once instead of after a long wait for a download that never starts
        self.page.on("download", _on_download)
        self.context.on("page", _on_new_page)
        try:
            self._real_click(button)
            start = monotonic()
            limit_s = self.cfg.get("timeouts.download_ms", 900000) / 1000
            next_log_s = PAGE_LOG_EVERY_S
            while not downloads:
                elapsed = monotonic() - start
                if elapsed > limit_s:
                    raise ScraperError(
                        f"no download started within {limit_s:.0f} seconds of clicking the "
                        "button, if a dialog is showing in the dump, send it"
                    )
                if elapsed >= next_log_s:
                    log.info("Still waiting for the export after %.0f seconds", elapsed)
                    self._log_browser_problems()
                    next_log_s += SYNC_LOG_EVERY_S
                self.page.wait_for_timeout(250)
        except ScraperError as e:
            self.dump_diagnostics("ng_export_download_failed")
            raise ScraperError(f"could not download the export: {e}") from e
        finally:
            self.page.remove_listener("download", _on_download)
            self.context.remove_listener("page", _on_new_page)

        download = downloads[0]
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        suggested = download.suggested_filename or "lmis_ng_export.xlsx"
        dest = download_dir / f"{ts}_{suggested}"
        download.save_as(str(dest))
        log.info("Saved downloaded export to %s", dest)
        return dest


def open_browser(cfg: Config) -> NigeriaScraper:
    """Factory matching the `with open_browser(cfg) as s:` usage."""
    return NigeriaScraper(cfg)
