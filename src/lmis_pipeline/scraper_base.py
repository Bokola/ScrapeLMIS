"""Shared Playwright driver infrastructure: browser lifecycle, diagnostics
dumping, and the resilient multi-frame selector matching used by every
country-specific scraper in this package.

Split out from the original single-country scraper.py when Malawi was
added, so the two country drivers (scraper.py for Mozambique/SIMAM,
malawi_scraper.py for Malawi) can share this without duplicating it, and so
neither country's fixes risk breaking the other's.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from time import monotonic, sleep

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


class BaseScraper:
    """Shared browser lifecycle, diagnostics, and selector-matching logic.
    Country-specific subclasses (LMISScraper for Mozambique, MalawiScraper
    for Malawi) add their own login/navigation/download methods on top of
    this - see those modules for country-specific behavior and what's
    confirmed vs. guessed for each.
    """

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
    def __enter__(self) -> "BaseScraper":
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(headless=self.cfg.headless)

        context_kwargs: dict = dict(
            viewport={"width": 1600, "height": 900},
            accept_downloads=True,
            user_agent=self.cfg.get("browser.user_agent", DEFAULT_USER_AGENT),
            locale=self.cfg.get("browser.locale", "en-US"),
            timezone_id=self.cfg.get("browser.timezone", "UTC"),
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
        call in a subclass should go through this method, not call
        page.wait_for_load_state directly."""
        assert self.page is not None
        try:
            self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PWTimeout:
            log.debug(
                "networkidle wait timed out after %dms - continuing anyway", timeout_ms
            )

    def _wait_for_loading_overlay_clear(self, timeout_ms: int | None = None) -> None:
        """Wait for a global loading-spinner modal to disappear, if one is
        currently showing (selector list: common.loading_overlay). Bounded
        and non-fatal.

        NOT CALLED ANYWHERE in the Mozambique flow - confirmed via a real
        diagnostics capture there that its own `.loading-modal` is
        aria-hidden="true" but never actually satisfies Playwright's
        "hidden" state, so waiting for it just burns the full timeout for
        nothing (see scraper.py's _login_once() for how that element is
        handled instead: clicking through it via _click()'s native-click
        approach). Kept here for a genuinely transient spinner if one
        turns up in Malawi's flow instead."""
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

        Confirmed necessary for Mozambique's SIMAM: it has a persistent,
        aria-hidden="true" overlay that genuinely occupies screen space in
        front of some elements. Playwright's click(force=True) skips its
        OWN pre-check for "is something covering this?", but still
        dispatches a real mouse event at the element's screen coordinates -
        if something really is on top at that exact point, the browser's
        native hit-testing can still deliver the click to the overlay
        instead of the intended element, with no error raised either way.

        Calling the element's own .click() via evaluate() bypasses
        coordinate-based hit-testing entirely, so it always fires on the
        intended node regardless of what's visually on top of it. Used
        uniformly across every country's scraper for consistency, even
        where it hasn't been proven necessary (e.g. Malawi) - it's a
        strict improvement over a coordinate-based click with no known
        downside for a normal, unobstructed element."""
        locator.evaluate("el => el.click()")

    def dump_diagnostics(self, tag: str) -> None:
        """On a selector failure: save a screenshot and dump the HTML of
        every frame (main page + any iframes), plus log every frame URL
        present. Dumping every frame costs nothing and covers both a
        same-document report (Malawi's native OpenLMIS reports) and an
        iframe-embedded one (Mozambique's Metabase dashboard)."""
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

        This matters a lot for Mozambique specifically: SIMAM's Requisition
        Data Report is a Metabase dashboard embedded in an iframe, so
        selectors for it only ever match inside that iframe, never the main
        page. Checking the main frame first with a full timeout per
        candidate before ever trying the iframe could burn minutes finding
        nothing, purely because of frame check order - not because
        anything was actually slow. Polling all frames together each cycle
        returns the instant a match appears anywhere, regardless of which
        frame it's in. Harmless overhead for Malawi, where everything is
        expected to live in the main frame (no iframes involved there).
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
