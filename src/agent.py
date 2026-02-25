"""LangGraph agent with OpenRouter LLM and plugin-based tools."""

import hashlib
import json
import logging
import os
import threading
import time
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.config import get_config
from langgraph.graph import END, START, MessagesState, StateGraph
from langchain_openrouter import ChatOpenRouter

from plugin_loader import load_plugins, TOOL_PLUGIN_TAGS

logger = logging.getLogger("agent")

_thread_local = threading.local()

LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "120"))  # seconds (converted to ms for SDK)
DEFAULT_MAX_TOOL_ROUNDS = int(os.environ.get("AGENT_DEFAULT_MAX_TOOL_ROUNDS", "10"))  # normal research
DEEP_RESEARCH_MAX_TOOL_ROUNDS = int(os.environ.get("AGENT_DEEP_MAX_TOOL_ROUNDS", "25"))  # when user asks for deep/long

TOOL_LOOP_WARN_THRESHOLD  = int(os.environ.get("TOOL_LOOP_WARN_THRESHOLD", "3"))
TOOL_LOOP_BLOCK_THRESHOLD = int(os.environ.get("TOOL_LOOP_BLOCK_THRESHOLD", "5"))
# Per-tool-name frequency limits (catches varied-args loops like browsing 7 different URLs)
TOOL_NAME_WARN_THRESHOLD  = int(os.environ.get("TOOL_NAME_WARN_THRESHOLD", "4"))
TOOL_NAME_BLOCK_THRESHOLD = int(os.environ.get("TOOL_NAME_BLOCK_THRESHOLD", "7"))
TOOL_NAME_WARN_OVERRIDES = {"browser": 10}
TOOL_NAME_BLOCK_OVERRIDES = {"browser": 20}
TOOL_RESULT_MAX_CHARS = int(os.environ.get("TOOL_RESULT_MAX_CHARS", "15000"))

# Effort → max tool rounds mapping
EFFORT_ROUNDS: dict[str, int] = {
    "quick": int(os.environ.get("AGENT_QUICK_MAX_TOOL_ROUNDS", "5")),
    "normal": DEFAULT_MAX_TOOL_ROUNDS,
    "deep": DEEP_RESEARCH_MAX_TOOL_ROUNDS,
}

# Model per effort level — empty means use OPENROUTER_MODEL
EFFORT_MODELS: dict[str, str] = {
    "quick": os.environ.get("OPENROUTER_MODEL_QUICK", "openai/gpt-oss-120b"),
    "normal": os.environ.get("OPENROUTER_MODEL_NORMAL", "deepseek/deepseek-v3.2"),
    "deep": os.environ.get("OPENROUTER_MODEL_DEEP", "z-ai/glm-5"),
}

EFFORT_MODELS_FALLBACK: dict[str, str] = {
    "quick":  os.environ.get("OPENROUTER_MODEL_QUICK_FALLBACK",  "deepseek/deepseek-v3.2"),
    "normal": os.environ.get("OPENROUTER_MODEL_NORMAL_FALLBACK", "deepseek/deepseek-v3.2"),
    "deep":   os.environ.get("OPENROUTER_MODEL_DEEP_FALLBACK",   "deepseek/deepseek-v3.2"),
}

# Optional: pass ChatOpenRouter's native reasoning.effort parameter to models that support it
# (e.g. Claude Sonnet 4.5, DeepSeek R1). Defaults to "none" (disabled) since the primary
# effort mechanism is model routing above. Set via env vars for reasoning-capable models.
# Values: "none", "minimal", "low", "medium", "high", "xhigh"
EFFORT_REASONING: dict[str, str] = {
    "quick":  os.environ.get("OPENROUTER_REASONING_QUICK",  "none"),
    "normal": os.environ.get("OPENROUTER_REASONING_NORMAL", "none"),
    "deep":   os.environ.get("OPENROUTER_REASONING_DEEP",   "none"),
}

TOOLS = load_plugins()
TOOLS_BY_NAME = {t.name: t for t in TOOLS}

SYSTEM_PROMPT_BASE = """You are a task assistant. Use available tools to look up or verify information when needed. Visit relevant URLs and summarize findings to answer the user's question.

## Task tracking rules (MANDATORY):
When you receive a task ID like [Your task ID is #N], you MUST follow these rules strictly:

1. **Set in_progress immediately**: Your very first action must be update_task(task_id=N, status="in_progress").

2. **Post frequent progress updates**: After every 1-2 tool calls, append a short note via update_task(task_id=N, notes="<what you just found>"). Keep each note to 1-3 sentences. Format notes so another agent could continue your work — include sources visited, key findings, approach taken, and what remains to be done. Examples:
   - "Visited example.com — found pricing: $10/mo basic, $50/mo pro. Still need to check competitor pricing."
   - "Searched X API docs — REST only, no official SDK found. Next: check GitHub issues for SDK requests."
   - "Compared A vs B — A has better docs and community support. Remaining: check performance benchmarks."

3. **Before marking completed — verify you ACTUALLY did the work**:
   - ONLY mark completed if you performed the requested action (posted, wrote, searched, called APIs, ran tools, etc.).
   - If you could only SUGGEST or PLAN what to do (but could not execute), mark FAILED with your suggestions in the notes.
   - If you need user action to continue (e.g. credentials, confirmation), use ask_human — do NOT complete.
   - Call: update_task(task_id=N, status="completed", notes="## Result\n<what you DID, not what you SUGGEST>").
   - Notes must describe actions taken and their outcomes, not just recommendations.

4. **Before marking failed — REQUIRED self-challenge**: Before giving up:
   a. Call update_task(task_id=N, notes="Considering failure — reason: <why>. Trying alternatives...")
   b. Actually attempt at least one alternative approach
   c. Only then: update_task(task_id=N, status="failed", notes="## Failed\nReason: <thorough explanation>\nApproaches tried: <list>\nSuggestions for follow-up: <if any>")

5. **Use telegram_send** to share key findings with the user mid-research.

Once you have enough information, respond with your final summary and recommendations without making further tool calls.
When editing project files, use the filesystem tools (read_file, write_file, search_replace_file, etc.) with the task's project_id so files live in that project's workspace. Prefer search_replace_file for targeted edits; if it reports the old string was not found, use write_file to replace the whole file.

## Browser tool (interactive):
Your `browser` tool can navigate web pages AND interact with them. Supported actions:
- navigate(url) — go to a URL, returns page text
- read() — get current page text
- inspect() — get simplified DOM tree with CSS selectors (use this to find what to click/type)
- click(selector) — click an element by CSS selector
- type(selector, text) — type text into an input field
- press_key(key) — press Enter, Tab, Escape, etc.
- scroll(direction, amount) — scroll up/down
- wait(selector) — wait for an element to appear

**Workflow for interactive tasks** (e.g. posting on social media):
1. navigate to the site
2. inspect to find form fields and buttons
3. type credentials / content into fields
4. click submit buttons
5. read to verify the result

If a site requires login credentials you don't have, use ask_human to request them.
Always use telegram_send to inform the user before performing sensitive actions (posting, purchasing, deleting).

## Credential vault (encrypted secrets):
Use the credential_vault tools to store and retrieve passwords or API tokens **per project**:
- store_credential(name, secret) — encrypts and saves a secret for the current task's project
- get_credential(name) — retrieves a previously stored secret for this project
- list_credentials() — lists available credential names (no values)
- delete_credential(name) — removes a stored secret

Workflow for login flows:
1. Before asking the human for credentials, call get_credential() with a stable name like "twitter/main" or "github/personal".
2. If a credential exists, use it directly in browser.type() or HTTP requests.
3. If none exists, call ask_human once to obtain the credential, then immediately call store_credential() so future tasks can log in without asking again.

Your available tools: browser (navigate, read, click, type, inspect), credential_vault (encrypted credential storage), memory_search/memory_add (RAG), update_task/create_task (task tracking), telegram_send (message user), ask_human (ask user a question), read_file/write_file/search_replace_file (project files).

## Rules for ask_human (STRICT):
- ONLY ask when you genuinely cannot proceed without user input.
- NEVER ask factual questions you could answer via web search or memory.
- NEVER ask "should I continue?" or "do you want more detail?" — just do your best.
- NEVER ask for confirmation of your approach — pick the best one and proceed.
- Valid reasons to ask: ambiguous personal preferences, credentials, genuinely unclear requirements.
- Before calling ask_human, check: "Could I make a reasonable assumption?" If yes, state it in a progress note and proceed.
- Maximum 1 question per task unless the answer creates genuine new ambiguity.

## Usage and safety
- **No malicious or harmful use:** Do not assist with malicious purposes, illegal activities, or anything intended to harm people, systems, or data.
- **No harmful code execution:** Do not suggest or request code or commands that could damage systems, exfiltrate data, gain unauthorized access, or introduce malware. Do not run destructive or unsafe commands (e.g. unbounded rm -rf, piping untrusted scripts).
- **Project scope only:** File and command operations are limited to the task's project workspace (enforced by tools). Do not attempt to read, write, or run commands outside that workspace; refuse and explain if asked.
- **Refusal:** If a request is harmful, illegal, or against these rules, refuse clearly and briefly; do not comply or elaborate with harmful instructions."""

DEEP_RESEARCH_KEYWORDS = (
    "deep research",
    "long thinking",
    "thorough research",
    "comprehensive research",
    "exhaustive",
    "deep dive",
    "extensive research",
    "as many sources",
    "every relevant",
)


def _is_deep_research(query: str) -> bool:
    """True if the user query explicitly asks for deep/long/thorough research."""
    q = (query or "").strip().lower()
    return any(kw in q for kw in DEEP_RESEARCH_KEYWORDS)


def _tool_call_hash(name: str, args: dict) -> str:
    """Deterministic hash for a tool call (name + sorted args)."""
    payload = f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _truncate_observation(text: str, max_chars: int = TOOL_RESULT_MAX_CHARS) -> str:
    """Truncate tool output to avoid blowing up the context window."""
    if len(text) <= max_chars:
        return text
    overflow = len(text) - max_chars
    return text[:max_chars] + f"\n\n[... truncated {overflow:,} chars]"


def _is_transient_llm_error(exc: Exception) -> bool:
    """Return True if the exception looks like a transient/retriable LLM error."""
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    name = type(exc).__name__
    if name in ("ReadTimeout", "ConnectTimeout", "RemoteProtocolError", "ConnectError"):
        return True
    s = str(exc)
    if any(code in s for code in ("429", "500", "502", "503", "504")):
        return True
    if any(kw in s.lower() for kw in ("timeout", "rate limit", "connection reset",
                                       "model not available", "no endpoints", "overloaded")):
        return True
    return False


def _create_llm(effort: str = "normal", model: str | None = None):
    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError(
            "OPENROUTER_API_KEY (or OPENAI_API_KEY) must be set. Add it to .env or export it."
        )
    # Per-effort model, falling back to the global default
    if model is None:
        default_model = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v3.2")
        model = EFFORT_MODELS.get(effort, "").strip() or default_model
    # Build reasoning config from effort level
    reasoning_effort = EFFORT_REASONING.get(effort, "").strip().lower()
    reasoning = None
    if reasoning_effort and reasoning_effort != "none":
        reasoning = {"effort": reasoning_effort}

    logger.info("Using model %s for effort=%s (reasoning=%s)", model, effort, reasoning_effort or "none")
    kwargs = dict(
        model=model,
        temperature=0,
        api_key=api_key,
        timeout=LLM_TIMEOUT * 1000,  # SDK expects milliseconds
        app_url=os.environ.get("YOUR_SITE_URL", "https://github.com/JustinGuese/openshrimp"),
        app_title="openShrimp",
    )
    if reasoning:
        kwargs["reasoning"] = reasoning
    llm = ChatOpenRouter(**kwargs)
    return llm, model


_EFFORT_GUIDANCE: dict[str, str] = {
    "quick": (
        "**Effort: QUICK** — Get to the answer fast. 1-2 web searches max. "
        "Do NOT ask the user questions. Prefer existing knowledge over searching."
    ),
    "normal": (
        "**Effort: NORMAL** — Balanced approach. Check 2-4 sources. "
        "Avoid repeating searches with similar queries. Do not use the same tool more than 5 times."
    ),
    "deep": (
        "**Effort: DEEP** — Research thoroughly. Cross-reference multiple sources. "
        "Vary tool usage. Do not use any single tool more than 10 times."
    ),
}


def _system_prompt(max_tool_rounds: int, effort: str = "normal") -> str:
    """Build system prompt with effort-aware guidance and tool-round limit."""
    prompt = SYSTEM_PROMPT_BASE
    guidance = _EFFORT_GUIDANCE.get(effort, _EFFORT_GUIDANCE["normal"])
    prompt += f"\n\n{guidance}"
    prompt += f"\nUse at most {max_tool_rounds} tool calls total; then summarize your answer without further tool use."
    return prompt


def _count_tool_rounds(messages: list) -> int:
    """Count completed tool rounds (messages with tool_calls)."""
    return sum(1 for msg in messages if getattr(msg, "tool_calls", None))


def _llm_call(state: MessagesState) -> dict:
    """LLM node: invoke model with tools and return the response message."""
    try:
        run_config = get_config()
        configurable = run_config.get("configurable") or {}
        max_rounds = configurable.get("max_tool_rounds") or DEFAULT_MAX_TOOL_ROUNDS
        effort = configurable.get("effort") or "normal"
    except RuntimeError:
        max_rounds = DEFAULT_MAX_TOOL_ROUNDS
        effort = "normal"
    system_content = _system_prompt(max_rounds, effort)
    used_rounds = _count_tool_rounds(state["messages"])
    remaining = max(0, max_rounds - used_rounds)
    budget_line = f"\n\nTool budget: {remaining}/{max_rounds} calls remaining."
    if remaining <= 2 and used_rounds > 0:
        budget_line += " You are running low. Wrap up and provide your final answer now."
    elif remaining == 0:
        budget_line += " You have NO calls left. Provide your final answer immediately."
    system_content += budget_line
    messages = [SystemMessage(content=system_content)] + list(state["messages"])
    total_chars = sum(len(str(getattr(m, "content", ""))) for m in messages)
    logger.info(
        "Calling LLM (OpenRouter) — messages=%d, context_chars=%d, timeout=%ds",
        len(messages),
        total_chars,
        LLM_TIMEOUT,
    )
    llm, primary_model = _create_llm(effort=effort)
    llm_with_tools = llm.bind_tools(TOOLS) if TOOLS else llm
    t0 = time.monotonic()
    try:
        response = llm_with_tools.invoke(messages)
    except Exception as llm_exc:
        fallback_model = EFFORT_MODELS_FALLBACK.get(effort)
        if fallback_model and fallback_model != primary_model and _is_transient_llm_error(llm_exc):
            logger.warning(
                "Primary model %s failed: %s. Falling back to %s.",
                primary_model, llm_exc, fallback_model,
            )
            fallback_llm, _ = _create_llm(effort=effort, model=fallback_model)
            llm_with_tools_fb = fallback_llm.bind_tools(TOOLS) if TOOLS else fallback_llm
            response = llm_with_tools_fb.invoke(messages)
        else:
            raise
    elapsed = time.monotonic() - t0
    logger.info(
        "LLM returned in %.1fs (tool_calls=%s)",
        elapsed,
        bool(getattr(response, "tool_calls", None)),
    )
    return {"messages": [response]}


def _tool_node(state: MessagesState) -> dict:
    """Execute tool calls from the last message and return ToolMessages."""
    last_message = state["messages"][-1]
    tool_calls = getattr(last_message, "tool_calls", None) or []
    result = []
    for tool_call in tool_calls:
        name = getattr(tool_call, "name", None) or (tool_call.get("name", "") if isinstance(tool_call, dict) else "")
        args = getattr(tool_call, "args", None)
        if args is None and isinstance(tool_call, dict):
            args = tool_call.get("args", {}) or {}
        args = args or {}
        call_id = getattr(tool_call, "id", None) or (tool_call.get("id", "") if isinstance(tool_call, dict) else "")
        tool = TOOLS_BY_NAME.get(name)
        blocked = False
        if tool is None:
            observation = f"Unknown tool: {name}"
            logger.warning("Unknown tool: %s", name)
        else:
            # --- Tool loop detection (identical args) ---
            tool_counts = getattr(_thread_local, "tool_call_counts", None)
            if tool_counts is None:
                tool_counts = {}
                _thread_local.tool_call_counts = tool_counts
            h = _tool_call_hash(name, args)
            tool_counts[h] = tool_counts.get(h, 0) + 1
            count = tool_counts[h]

            # --- Per-tool-name frequency detection (varied args) ---
            tool_name_counts = getattr(_thread_local, "tool_name_counts", None)
            if tool_name_counts is None:
                tool_name_counts = {}
                _thread_local.tool_name_counts = tool_name_counts
            tool_name_counts[name] = tool_name_counts.get(name, 0) + 1
            name_count = tool_name_counts[name]

            if count >= TOOL_LOOP_BLOCK_THRESHOLD:
                logger.warning(
                    "Tool loop BLOCKED: %s called %d times with identical args", name, count,
                )
                observation = (
                    f"BLOCKED: You have called {name} with identical arguments {count} times. "
                    "This looks like an infinite loop. Try a different approach or tool."
                )
                blocked = True
            elif name_count >= TOOL_NAME_BLOCK_OVERRIDES.get(name, TOOL_NAME_BLOCK_THRESHOLD):
                logger.warning(
                    "Tool frequency BLOCKED: %s called %d times total", name, name_count,
                )
                observation = (
                    f"BLOCKED: You have called {name} {name_count} times this session. "
                    "You are stuck in a loop. Stop using this tool and either: "
                    "(1) use a DIFFERENT tool, (2) provide your final answer, or "
                    "(3) mark the task as failed explaining what you could not do."
                )
                blocked = True
            else:
                logger.info("Invoking tool: %s with args: %s", name, args)
                try:
                    t0 = time.monotonic()
                    observation = tool.invoke(args)
                    elapsed = time.monotonic() - t0
                    logger.info(
                        "Tool %s finished in %.1fs (result length=%s)",
                        name,
                        elapsed,
                        len(str(observation)),
                    )
                except Exception as e:
                    logger.exception("Tool %s error: %s", name, e)
                    observation = f"Tool error: {e!r}"

                if count >= TOOL_LOOP_WARN_THRESHOLD:
                    logger.warning(
                        "Tool loop WARNING: %s called %d times with identical args", name, count,
                    )
                    observation = str(observation) + (
                        f"\n\n⚠️ WARNING: You have called {name} with identical arguments "
                        f"{count} times. Vary your approach or move on."
                    )
                elif name_count >= TOOL_NAME_WARN_OVERRIDES.get(name, TOOL_NAME_WARN_THRESHOLD):
                    logger.warning(
                        "Tool frequency WARNING: %s called %d times total", name, name_count,
                    )
                    observation = str(observation) + (
                        f"\n\n⚠️ WARNING: You have called {name} {name_count} times this session. "
                        "Consider whether you are making progress. If not, try a different "
                        "approach or wrap up with what you have."
                    )
        # Auto-save to memory after research tools (plugin has "research" tag)
        # Skip if the call was blocked (nothing useful to save)
        if not blocked and "research" in TOOL_PLUGIN_TAGS.get(name, []):
            obs_str = str(observation).strip()
            skip_error_prefixes = ("Tool error", "[memory_rag ERROR]", "[browser ERROR]")
            if not any(obs_str.startswith(p) for p in skip_error_prefixes):
                memory_add_tool = TOOLS_BY_NAME.get("memory_add")
                if memory_add_tool:
                    content = obs_str[:12_000]
                    source = args.get("url", "") if isinstance(args, dict) else ""
                    try:
                        memory_add_tool.invoke({"content": content, "source": source})
                        logger.info("Auto-saved research tool result to memory (tool=%s)", name)
                    except Exception as mem_err:
                        logger.warning("Auto-save to memory failed (tool=%s): %s", name, mem_err)
        # Truncate observation to avoid blowing up context
        obs_str = _truncate_observation(str(observation))
        result.append(ToolMessage(content=obs_str, tool_call_id=call_id))
        _cb = getattr(_thread_local, "on_progress", None)
        if _cb:
            try:
                _cb(name, args, str(observation))
            except Exception:
                pass

    # Heartbeat: update the task's heartbeat_at after each round of tool calls
    try:
        import telegram_state
        import task_service as _task_service
        task_id = telegram_state.get_task_id()
        if task_id:
            _task_service.update_heartbeat(task_id)
    except Exception:
        pass

    return {"messages": result}


def _should_continue(state: MessagesState):
    """Route to tool_node if the last message has tool_calls, else END."""
    messages = state["messages"]
    if not messages:
        return END
    last = messages[-1]
    if getattr(last, "tool_calls", None):
        return "tool_node"
    return END


def create_research_agent():
    """Build and return a compiled LangGraph agent with tool-calling and browser interaction."""
    builder = StateGraph(MessagesState)
    builder.add_node("llm_call", _llm_call)
    builder.add_node("tool_node", _tool_node)
    builder.add_edge(START, "llm_call")
    builder.add_conditional_edges("llm_call", _should_continue, ["tool_node", END])
    builder.add_edge("tool_node", "llm_call")
    return builder.compile(checkpointer=None, interrupt_before=None, interrupt_after=None, debug=False)


def _recursion_limit(max_tool_rounds: int) -> int:
    # LangGraph: START→llm, then N times (tool→llm→tool), then one more llm for final answer
    return 2 + max_tool_rounds * 2


def _fallback_summary(query: str, messages: list) -> str:
    """Produce a best-effort summary after hitting the recursion limit."""
    tool_contents = [
        msg.content[:3000]
        for msg in messages
        if isinstance(msg, ToolMessage) and msg.content
    ]
    if not tool_contents:
        return "Research hit the tool-call limit before gathering enough data to answer."
    gathered = "\n\n---\n\n".join(tool_contents)
    summary_prompt = (
        f"You were researching the following query but hit the tool-call limit.\n"
        f"Query: {query}\n\n"
        f"Here is the information gathered so far:\n\n{gathered}\n\n"
        f"Based only on this information, give the best answer you can. "
        f"If you don't have enough to answer fully, say so and share what you found."
    )
    logger.info("Calling LLM for fallback summary (%d tool results).", len(tool_contents))
    llm, _ = _create_llm(effort="normal")
    response = llm.invoke([HumanMessage(content=summary_prompt)])
    result = getattr(response, "content", str(response)) or ""
    logger.info("Fallback summary length=%d", len(result))
    return f"⚠️ Hit tool-call limit; here's a summary of what was found:\n\n{result}"


def run_agent(query: str, on_progress=None, effort: str = "normal") -> str:
    """Run the task agent on a query and return the final assistant message content.

    Args:
        query: The task or research query.
        on_progress: Optional callback(tool_name, args, observation) called after each tool.
        effort: One of "quick", "normal", "deep". Auto-upgrades to "deep" if deep keywords found.
    """
    _thread_local.on_progress = on_progress
    _thread_local.tool_call_counts = {}
    _thread_local.tool_name_counts = {}
    # Auto-upgrade: if user asked for deep research but effort wasn't explicitly set to deep
    if effort == "normal" and _is_deep_research(query):
        effort = "deep"
    max_rounds = EFFORT_ROUNDS.get(effort, DEFAULT_MAX_TOOL_ROUNDS)
    logger.info("Invoking task agent... (max_tool_rounds=%s, effort=%s)", max_rounds, effort)
    agent = create_research_agent()
    config = {
        "recursion_limit": _recursion_limit(max_rounds),
        "configurable": {"max_tool_rounds": max_rounds, "effort": effort},
    }

    # Stream so we can capture accumulated state on recursion-limit error.
    all_messages: list = [HumanMessage(content=query)]
    try:
        for chunk in agent.stream(
            {"messages": [HumanMessage(content=query)]},
            config=config,
            stream_mode="values",
        ):
            msgs = chunk.get("messages") or []
            if msgs:
                all_messages = list(msgs)
    except Exception as exc:
        if type(exc).__name__ == "GraphRecursionError":
            logger.warning(
                "Recursion limit hit after %d messages; falling back to summary.", len(all_messages)
            )
            return _fallback_summary(query, all_messages)
        raise

    if not all_messages:
        logger.warning("Agent returned no messages")
        return ""
    last = all_messages[-1]
    out = getattr(last, "content", str(last)) or ""
    logger.info("Agent done (final message length=%s)", len(out))
    return out
