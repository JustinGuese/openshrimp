"""Task-scoped playbooks: injected into the query when task keywords match.

Playbooks provide domain-specific guidance to the agent without bloating the
system prompt. Each playbook is only injected when its keyword pattern matches
the user's task description.
"""

from __future__ import annotations
import re

# Each entry: (compiled regex, playbook text)
_PLAYBOOKS: list[tuple[re.Pattern, str]] = []


def _register(pattern: str, text: str) -> None:
    _PLAYBOOKS.append((re.compile(pattern, re.IGNORECASE), text.strip()))


# ── X.com / Twitter ──────────────────────────────────────────────────────────
_register(
    r"\bx\.com\b|\btwitter\b|\btweet\b|\bpost on x\b",
    """
[X.com Playbook]
Login flow (x.com uses a multi-step form):
1. browser(action="navigate", url="https://x.com/i/flow/login")
2. browser(action="inspect") → find the email/phone input (often data-testid="ocfEnterTextTextInput" or input[autocomplete="username"])
3. browser(action="type", selector="input[autocomplete='username']", text="<email>")
4. browser(action="click", selector="[role='button']:has-text('Next')") or press Enter
5. browser(action="inspect") → find password input (input[name="password"] or data-testid="ocfEnterTextTextInput")
6. browser(action="type", selector="input[name='password']", text="<password>")
7. browser(action="click", selector="[data-testid='LoginForm_Login_Button']") or press Enter
8. browser(action="read") → check for "unusual activity" page → if present, follow confirmation steps
9. To post: browser(action="navigate", url="https://x.com/compose/post") → type → click Post button
Credentials: search memory for "x.com" or "twitter" credentials before asking the user.
Cookies are saved automatically — if already logged in from a previous task, navigate directly to x.com and verify.
""",
)

# ── Credential memory protocol ────────────────────────────────────────────────
_register(
    r"\bcredential\b|\blogin\b|\bpassword\b|\bpass\b|\busername\b|\bsign.?in\b",
    """
[Credential Protocol]
Before asking the user for credentials:
1. Call memory_search(query="<site name> credentials") to check if they were previously shared.
2. If found, use them directly — do NOT ask the user.
3. If the user provides credentials in this task, immediately call memory_add(content="<site> credentials: username=<u> password=<p>", source="credentials:<site>") so they are remembered for future tasks.
""",
)


def detect(query: str) -> str:
    """Return all matching playbook sections joined, or empty string if none match."""
    matched = [text for pattern, text in _PLAYBOOKS if pattern.search(query)]
    return "\n\n".join(matched)
