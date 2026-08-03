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
    user_agent: str | None = None
    session_validator: Callable[[Any], bool] | None = None
    headless: bool = False
    login_timeout_seconds: float = 60.0


def ensure_browser_session(
    config: BrowserSessionConfig,
    auth_extractor: Callable[[Any, str, str], Any],
) -> Any:
    """Load a persistent browser session and return fresh auth material.

    A required cookie and optional page-state validator must both confirm the session before
    extracted credentials are returned.
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
                launch_options = {
                    "headless": config.headless,
                    "args": ["--disable-blink-features=AutomationControlled"],
                }
                if config.user_agent:
                    launch_options["user_agent"] = config.user_agent
                context = p.chromium.launch_persistent_context(str(config.session_dir), **launch_options)
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    # Loading the origin lets Chromium process session renewal and account state
                    # before its cookies are copied into API headers.
                    page.goto(config.origin, wait_until="domcontentloaded", timeout=60_000)
                    user_agent = safe_page_user_agent(page)
                    cookie_header = playwright_cookie_header(context.cookies([config.origin]))

                    def session_is_ready() -> bool:
                        return bool(cookie_value(cookie_header, config.required_cookie)) and (
                            config.session_validator is None or config.session_validator(page)
                        )

                    if not session_is_ready():
                        print(
                            f"{config.label} login required. Complete login in the opened browser window; "
                            "this browser profile will be reused on future runs.",
                            file=sys.stderr,
                        )

                    while not session_is_ready() and time.time() < deadline:
                        page.wait_for_timeout(2_000)
                        cookie_header = playwright_cookie_header(context.cookies([config.origin]))
                        user_agent = safe_page_user_agent(page)

                    if not session_is_ready():
                        raise RuntimeError(
                            f"{config.label} session is not logged in or usable. "
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
