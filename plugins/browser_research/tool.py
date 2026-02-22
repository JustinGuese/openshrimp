"""Browser research tool for openshrimp plugin system.

Uses browserless Chrome with pyppeteer-stealth for web research.
Stealth: BROWSERLESS_USE_STEALTH defaults to 1 (use Browserless /stealth route for
server-side anti-detection). Set to 0 to disable. Client-side evasions are always
applied via pyppeteer-stealth; optional BROWSER_USER_AGENT tunes the fingerprint.
"""

import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from langchain_core.tools import tool
from pyppeteer import connect, launch
from pyppeteer_stealth import stealth

# Add src directory to path so we can import schemas when run standalone
_src = Path(__file__).resolve().parents[2] / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from schemas import ToolResult


def _browserless_ws_url() -> str:
    raw = os.environ.get("BROWSERLESS_WS_URL", "http://localhost:3000").strip()
    token = os.environ.get("BROWSERLESS_TOKEN", "").strip()
    use_stealth = os.environ.get("BROWSERLESS_USE_STEALTH", "1").strip().lower() in ("1", "true", "yes")
    solve_captchas = (
        os.environ.get("BROWSERLESS_SOLVE_CAPTCHAS", "1").strip().lower() in ("1", "true", "yes")
        or bool(os.environ.get("CAPSOLVER_API_KEY", "").strip())
    )
    parsed = urlparse(raw)
    if parsed.scheme == "http":
        netloc = parsed.netloc or "localhost:3000"
        scheme = "ws"
    elif parsed.scheme == "https":
        netloc = parsed.netloc or "localhost:3000"
        scheme = "wss"
    else:
        scheme, netloc = parsed.scheme, parsed.netloc or "localhost:3000"
    path = parsed.path or "/"
    if use_stealth:
        path = "/stealth"
    query = parsed.query
    if token:
        query = f"{query}&token={token}" if query else f"token={token}"
    if solve_captchas:
        query = f"{query}&solveCaptchas=true" if query else "solveCaptchas=true"
    return urlunparse((scheme, netloc, path, parsed.params, query, parsed.fragment))


async def _browser_research_async(url: str, instruction: str) -> str:
    ws_url = _browserless_ws_url()
    browser = None
    try:
        browser = await connect(browserWSEndpoint=ws_url)
    except Exception as e:
        try:
            browser = await launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
        except Exception as e2:
            return f"Failed to connect to browser (tried remote and local): {e!r}; {e2!r}"

    try:
        page = await browser.newPage()
        user_agent = os.environ.get("BROWSER_USER_AGENT", "").strip() or None
        await stealth(page, user_agent=user_agent)
        await page.setViewport({"width": 1280, "height": 800})
        await page.goto(url, {"waitUntil": "networkidle2", "timeout": 30000})

        # Optional: when Browserless CAPTCHA solving is enabled, wait briefly for captcha detection and solve
        solve_captchas = (
            os.environ.get("BROWSERLESS_SOLVE_CAPTCHAS", "1").strip().lower() in ("1", "true", "yes")
            or bool(os.environ.get("CAPSOLVER_API_KEY", "").strip())
        )
        if solve_captchas:
            try:
                cdp = await page.target.createCDPSession()
                captcha_future = asyncio.get_running_loop().create_future()

                def _on_captcha_found(*_args):
                    if not captcha_future.done():
                        captcha_future.set_result(True)

                cdp.on("Browserless.captchaFound", _on_captcha_found)
                try:
                    await asyncio.wait_for(captcha_future, timeout=3.0)
                except asyncio.TimeoutError:
                    pass
                else:
                    await cdp.send("Browserless.solveCaptcha")
                    await asyncio.sleep(2)
            except Exception:
                pass

        # Extract main text content; optional instruction can be used for future extensions (e.g. click, scroll)
        result = await page.evaluate(
            """
            () => {
                const body = document.body;
                if (!body) return '';
                const clone = body.cloneNode(true);
                const scripts = clone.querySelectorAll('script, style, nav, footer, header');
                scripts.forEach(el => el.remove());
                return (clone.innerText || clone.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 15000);
            }
            """
        )
        # Close page then browser so the connection doesn't get "Page crashed!" in a background task later
        await _close_page_and_browser(page, browser)
        return result or "(no extractable text)"
    except Exception as e:
        if browser:
            try:
                pages = await browser.pages()
                for p in pages:
                    await _close_page_and_browser(p, None)
                await _close_page_and_browser(None, browser)
            except Exception:
                pass
        return f"Browser error: {e!r}"


async def _close_page_and_browser(page=None, browser=None):
    """Close page and/or browser, swallowing post-close errors (e.g. Page crashed) so they don't surface as unhandled task exceptions."""
    if page:
        try:
            await page.close()
        except Exception:
            pass
    if browser:
        try:
            await browser.close()
        except Exception:
            pass


@tool
def browser_research(url: str, instruction: str = "") -> str:
    """Visit a URL in a headless browser and extract main text content for research.

    Use this when you need to read the current content of a webpage (e.g. to research
    a product, API, or article). The page is loaded with anti-detection (stealth).
    Provide the full URL and an optional instruction (e.g. what to look for).

    Args:
        url: Full URL to open (e.g. https://example.com).
        instruction: Optional instruction describing what to find or do on the page (for context).
    """
    raw = asyncio.run(_browser_research_async(url, instruction))
    is_error = raw.startswith(("Failed to connect", "Browser error:", "(no extractable"))
    result = ToolResult(
        status="error" if is_error else "ok",
        data=raw,
        plugin="browser_research",
        extra={"url": url, "instruction": instruction},
    )
    return result.to_string()


# Public contract for plugin loader
TOOLS = [browser_research]
