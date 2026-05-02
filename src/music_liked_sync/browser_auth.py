import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .utils import (
    browser_session_lock,
    cookie_value,
    playwright_cookie_header,
    safe_page_user_agent,
)


@dataclass(frozen=True)
class BrowserSessionConfig:
    session_dir: Path
    origin: str
    required_cookie: str
    lock_path: Path
    label: str
    headless: bool = False
    login_timeout_seconds: float = 60.0


def ensure_browser_session(
    config: BrowserSessionConfig,
    auth_extractor: Callable[[Any, str, str], Any],
) -> Any:
    """Shared browser auth orchestration for Spotify and YTM.

    Launches a persistent Chromium context, waits for the required cookie,
    then delegates auth-state extraction to ``auth_extractor(page, cookie_header, user_agent)``.
    """
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            f"{config.label} auth requires Playwright. Run: uv sync && uv run playwright install chromium"
        ) from exc

    config.session_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + config.login_timeout_seconds

    try:
        with browser_session_lock(config.lock_path):
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    str(config.session_dir),
                    headless=config.headless,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    user_agent = safe_page_user_agent(page)
                    cookie_header = playwright_cookie_header(context.cookies([config.origin]))

                    if not cookie_value(cookie_header, config.required_cookie):
                        page.goto(config.origin, wait_until="domcontentloaded", timeout=60_000)
                        user_agent = safe_page_user_agent(page)
                        print(
                            f"{config.label} login required. Complete login in the opened browser window; "
                            "this browser profile will be reused on future runs.",
                            file=sys.stderr,
                        )

                    while not cookie_value(cookie_header, config.required_cookie) and time.time() < deadline:
                        page.wait_for_timeout(2_000)
                        cookie_header = playwright_cookie_header(context.cookies([config.origin]))
                        user_agent = safe_page_user_agent(page)

                    if not cookie_value(cookie_header, config.required_cookie):
                        raise RuntimeError(
                            f"{config.label} session is not logged in; missing {config.required_cookie}. "
                            f"Complete {config.label} login in the opened browser window, then rerun."
                        )

                    result = auth_extractor(page, cookie_header, user_agent)
                    print(f"{config.label} auth refreshed from persistent browser session", file=sys.stderr)
                    return result
                finally:
                    context.close()
    except PlaywrightError as exc:
        raise RuntimeError(
            f"Could not open the persistent {config.label} browser session. "
            "If this is the first run, install the browser with: uv run playwright install chromium"
        ) from exc
