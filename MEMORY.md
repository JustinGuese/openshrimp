# Project memory

Quick reference for key files and architecture.

## Key files

| Path | Purpose |
|------|---------|
| `src/agent.py` | LangGraph task agent; `run_agent()`, tool loop, system prompt |
| `src/browser.py` | Core browser session (thread-local), `execute_action()`, `close_session()` |
| `src/telegram_bot.py` | Telegram frontend; runs `run_agent()` in thread pool, calls `browser.close_session()` after each task |
| `src/plugins/browser/` | Browser plugin: thin wrapper around `browser.execute_action`; tool name `browser` |
| `src/plugins/plugin_loader.py` | Loads plugins from `src/plugins/<name>/` (manifest.json + tool.py) |

## Browser tool

- **Plugin name:** `browser` (replaces legacy `browser_research`).
- **Core logic:** `src/browser.py` — thread-local persistent session, one browser/page per task thread.
- **Actions:** navigate, read, inspect, click, type, press_key, scroll, wait. Returns `{ok, data, url, title}`.
- **Interactive use:** Agent can log in, fill forms, click, type, and submit — not just extract text. Use `inspect` to discover CSS selectors on unknown pages.
