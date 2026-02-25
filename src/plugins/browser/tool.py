"""Interactive browser tool for openshrimp plugin system.

Uses browserless Chrome with pyppeteer-stealth. Core logic lives in src/browser.py.
Actions: navigate, read, inspect, click, type, press_key, scroll, wait.
"""

import sys
from pathlib import Path

from langchain_core.tools import tool

_src = Path(__file__).resolve().parents[2]
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from browser import execute_action, close_session
from schemas import ToolResult


@tool
def browser(
    action: str,
    url: str = "",
    selector: str = "",
    text: str = "",
    key: str = "",
    direction: str = "down",
    amount: int = 500,
    timeout_ms: int = 10000,
) -> str:
    """Use the browser to navigate and interact with web pages.

    Supported actions:
    - navigate(url) — go to a URL, returns page text
    - read() — get current page text
    - inspect() — get simplified DOM tree with CSS selectors (use to find what to click/type)
    - click(selector) — click an element by CSS selector
    - type(selector, text) — type text into an input field
    - press_key(key) — press Enter, Tab, Escape, etc.
    - scroll(direction, amount) — scroll up/down/left/right
    - wait(selector, timeout_ms) — wait for an element to appear

    Workflow for interactive tasks (e.g. posting): 1) navigate to site 2) inspect to find
    form fields and buttons 3) type credentials/content 4) click submit 5) read to verify.

    For login flows, first try to retrieve credentials from the credential_vault plugin
    (get_credential). Only if no credential exists should you call ask_human to request
    credentials, then immediately store them with credential_vault.store_credential so
    future tasks can log in without asking again.
    """
    result = execute_action(
        action=action,
        url=url,
        selector=selector,
        text=text,
        key=key,
        direction=direction,
        amount=amount,
        timeout_ms=timeout_ms,
    )
    tr = ToolResult(
        status="error" if not result.get("ok") else "ok",
        data=result.get("data", ""),
        plugin="browser",
        extra={"url": result.get("url", ""), "title": result.get("title", "")},
    )
    return tr.to_string()


# For telegram_bot / other callers that need cleanup
def close_browser_session() -> None:
    close_session()


TOOLS = [browser]
