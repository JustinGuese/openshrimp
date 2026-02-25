"""Core browser session management and interactive actions.

Thread-local persistent session: one browser/page per task thread. Sync entry point
execute_action() dispatches to async implementation via the thread's event loop.
"""

import asyncio
import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from pyppeteer import connect, launch
from pyppeteer_stealth import stealth

_local = threading.local()


def _cookies_dir(domain: str) -> Path:
    """Return path to cookie file for the current user + domain."""
    import telegram_state
    tg_user_id = telegram_state.get_telegram_user_id() or "global"
    base = Path(os.environ.get("WORKSPACE_ROOT", "workspaces"))
    path = base / str(tg_user_id) / "cookies"
    path.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.-]", "_", domain)
    return path / f"{safe}.json"


async def _save_cookies(page) -> None:
    try:
        cookies = await page.cookies()
        domain = urlparse(page.url).netloc
        if domain:
            _cookies_dir(domain).write_text(json.dumps(cookies))
    except Exception:
        pass


async def _load_cookies(page, url: str) -> None:
    try:
        domain = urlparse(url).netloc
        if not domain:
            return
        path = _cookies_dir(domain)
        if path.exists():
            cookies = json.loads(path.read_text())
            if cookies:
                await page.setCookie(*cookies)
    except Exception:
        pass


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


def _get_loop() -> asyncio.AbstractEventLoop:
    """Create or return the thread's persistent event loop."""
    if not hasattr(_local, "loop") or _local.loop is None or _local.loop.is_closed():
        _local.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_local.loop)
    return _local.loop


async def _maybe_solve_captcha(page) -> None:
    solve_captchas = (
        os.environ.get("BROWSERLESS_SOLVE_CAPTCHAS", "1").strip().lower() in ("1", "true", "yes")
        or bool(os.environ.get("CAPSOLVER_API_KEY", "").strip())
    )
    if not solve_captchas:
        return
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


def _extract_text_js() -> str:
    return """
    () => {
        const body = document.body;
        if (!body) return '';
        const clone = body.cloneNode(true);
        const scripts = clone.querySelectorAll('script, style, nav, footer, header');
        scripts.forEach(el => el.remove());
        return (clone.innerText || clone.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 15000);
    }
    """


async def _extract_text(page) -> str:
    result = await page.evaluate(_extract_text_js())
    return result or "(no extractable text)"


def _extract_dom_summary_js() -> str:
    return """
    () => {
        const interactive = ['a', 'button', 'input', 'select', 'textarea'];
        const out = [];
        const walk = (el, depth) => {
            if (depth > 8 || out.length >= 500) return;
            const tag = (el.tagName || '').toLowerCase();
            const id = el.id ? '#' + el.id : '';
            const cls = el.className && typeof el.className === 'string'
                ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.')
                : '';
            const sel = tag + id + (cls || '');
            const attrs = {};
            if (el.name) attrs.name = el.name;
            if (el.placeholder) attrs.placeholder = el.placeholder;
            if (el.href) attrs.href = el.href;
            if (el.type) attrs.type = el.type;
            const summary = { tag, selector: sel };
            if (Object.keys(attrs).length) summary.attrs = attrs;
            if (interactive.includes(tag) || id || el.getAttribute('role')) {
                out.push(summary);
            }
            for (const child of el.children || []) walk(child, depth + 1);
        };
        walk(document.body || document.documentElement, 0);
        return JSON.stringify(out.slice(0, 200));
    }
    """


async def _extract_dom_summary(page) -> str:
    try:
        return await page.evaluate(_extract_dom_summary_js())
    except Exception as e:
        return f"DOM summary error: {e!r}"


async def _get_or_create_session():
    """Connect browser and create page if needed; verify page is alive."""
    if getattr(_local, "browser", None) and getattr(_local, "page", None):
        try:
            await _local.page.evaluate("1")
            return _local.browser, _local.page
        except Exception:
            pass
    if getattr(_local, "page", None):
        try:
            await _local.page.close()
        except Exception:
            pass
        _local.page = None
    if getattr(_local, "browser", None):
        try:
            await _local.browser.close()
        except Exception:
            pass
        _local.browser = None

    ws_url = _browserless_ws_url()
    try:
        _local.browser = await connect(browserWSEndpoint=ws_url)
    except Exception as e:
        try:
            _local.browser = await launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
        except Exception as e2:
            raise RuntimeError(f"Failed to connect to browser: {e!r}; {e2!r}") from e2

    _local.page = await _local.browser.newPage()
    user_agent = os.environ.get("BROWSER_USER_AGENT", "").strip() or None
    await stealth(_local.page, user_agent=user_agent)
    await _local.page.setViewport({"width": 1280, "height": 800})
    return _local.browser, _local.page


def close_session() -> None:
    """Close browser and page for this thread. Call after task ends (e.g. from telegram_bot)."""
    loop = getattr(_local, "loop", None)
    if loop is None or loop.is_closed():
        return
    async def _close():
        if getattr(_local, "page", None):
            try:
                await _local.page.close()
            except Exception:
                pass
            _local.page = None
        if getattr(_local, "browser", None):
            try:
                await _local.browser.close()
            except Exception:
                pass
            _local.browser = None
    try:
        loop.run_until_complete(_close())
    except Exception:
        pass


def _result(ok: bool, data: str, url: str = "", title: str = "") -> dict:
    return {"ok": ok, "data": data, "url": url, "title": title}


async def _run_action(
    action: str,
    url: str = "",
    selector: str = "",
    text: str = "",
    key: str = "",
    direction: str = "down",
    amount: int = 500,
    timeout_ms: int = 10000,
) -> dict:
    browser, page = await _get_or_create_session()
    current_url = page.url
    current_title = await page.title()

    if action == "navigate":
        if not url:
            return _result(False, "navigate requires url", current_url, current_title)
        try:
            await _load_cookies(page, url)
            await page.goto(url, {"waitUntil": "networkidle2", "timeout": 30000})
            await _maybe_solve_captcha(page)
            await _save_cookies(page)
            content = await _extract_text(page)
            return _result(True, content, page.url, await page.title())
        except Exception as e:
            return _result(False, f"Navigate error: {e!r}", current_url, current_title)

    if action == "read":
        try:
            content = await _extract_text(page)
            return _result(True, content, page.url, await page.title())
        except Exception as e:
            return _result(False, f"Read error: {e!r}", current_url, current_title)

    if action == "inspect":
        try:
            summary = await _extract_dom_summary(page)
            return _result(True, summary, page.url, await page.title())
        except Exception as e:
            return _result(False, f"Inspect error: {e!r}", current_url, current_title)

    if action == "click":
        if not selector:
            return _result(False, "click requires selector", current_url, current_title)
        try:
            await page.click(selector, timeout=timeout_ms or 10000)
            await asyncio.sleep(0.5)
            await _save_cookies(page)
            content = await _extract_text(page)
            return _result(True, f"Clicked {selector}.\n\n{content}", page.url, await page.title())
        except Exception as e:
            return _result(False, f"Click error: {e!r}", current_url, current_title)

    if action == "type":
        if not selector:
            return _result(False, "type requires selector", current_url, current_title)
        try:
            await page.click(selector, timeout=timeout_ms or 10000)
            await page.keyboard.type(text, delay=50)
            return _result(True, f"Typed into {selector}.", page.url, await page.title())
        except Exception as e:
            return _result(False, f"Type error: {e!r}", current_url, current_title)

    if action == "press_key":
        if not key:
            return _result(False, "press_key requires key (e.g. Enter, Tab, Escape)", current_url, current_title)
        try:
            await page.keyboard.press(key)
            await _save_cookies(page)
            return _result(True, f"Pressed {key}.", page.url, await page.title())
        except Exception as e:
            return _result(False, f"Press key error: {e!r}", current_url, current_title)

    if action == "scroll":
        try:
            dx = 0
            dy = amount if direction == "down" else (-amount if direction == "up" else amount)
            if direction in ("left", "right"):
                dx = amount if direction == "right" else -amount
                dy = 0
            await page.evaluate(f"window.scrollBy({{ left: {dx}, top: {dy} }})")
            await asyncio.sleep(0.3)
            content = await _extract_text(page)
            return _result(True, content, page.url, await page.title())
        except Exception as e:
            return _result(False, f"Scroll error: {e!r}", current_url, current_title)

    if action == "wait":
        if not selector:
            return _result(False, "wait requires selector", current_url, current_title)
        try:
            t = timeout_ms or 10000
            await page.waitForSelector(selector, {"timeout": t})
            return _result(True, f"Element {selector} appeared.", page.url, await page.title())
        except Exception as e:
            return _result(False, f"Wait error: {e!r}", current_url, current_title)

    if action == "clear_cookies":
        try:
            domain = urlparse(page.url).netloc
            if domain:
                _cookies_dir(domain).unlink(missing_ok=True)
                return _result(True, f"Cookies cleared for {domain}.", page.url, await page.title())
            return _result(False, "No domain available to clear cookies for.", current_url, current_title)
        except Exception as e:
            return _result(False, f"Clear cookies error: {e!r}", current_url, current_title)

    return _result(False, f"Unknown action: {action}", current_url, current_title)


def execute_action(
    action: str,
    url: str = "",
    selector: str = "",
    text: str = "",
    key: str = "",
    direction: str = "down",
    amount: int = 500,
    timeout_ms: int = 10000,
) -> dict:
    """Sync entry: run the given browser action in this thread's session. Returns {ok, data, url, title}."""
    loop = _get_loop()
    return loop.run_until_complete(
        _run_action(
            action=action,
            url=url,
            selector=selector,
            text=text,
            key=key,
            direction=direction,
            amount=amount,
            timeout_ms=timeout_ms,
        )
    )
