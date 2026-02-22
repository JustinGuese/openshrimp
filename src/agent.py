"""LangGraph agent with OpenRouter LLM and plugin-based tools."""

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

TOOLS = load_plugins()
TOOLS_BY_NAME = {t.name: t for t in TOOLS}

SYSTEM_PROMPT_BASE = """You are a research assistant. Use available tools to look up or verify information when needed. Visit relevant URLs and summarize findings to answer the user's question.

## Task tracking rules (MANDATORY):
When you receive a task ID like [Your task ID is #N], you MUST follow these rules strictly:

1. **Set in_progress immediately**: Your very first action must be update_task(task_id=N, status="in_progress").

2. **Post frequent progress updates**: After every 1-2 tool calls, append a short note via update_task(task_id=N, notes="<what you just found>"). Keep each note to 1-3 sentences. Examples:
   - "Visited example.com — found pricing: $10/mo basic, $50/mo pro."
   - "Searched X API docs — REST only, no official SDK found."
   - "Compared A vs B — A has better docs and community support."

3. **Before marking completed — REQUIRED final summary**: You MUST call update_task(task_id=N, status="completed", notes="## Result\n<thorough summary with findings, conclusions, and recommendations>"). Never set status to "completed" without notes containing a real result.

4. **Before marking failed — REQUIRED self-challenge**: Before giving up:
   a. Call update_task(task_id=N, notes="Considering failure — reason: <why>. Trying alternatives...")
   b. Actually attempt at least one alternative approach
   c. Only then: update_task(task_id=N, status="failed", notes="## Failed\nReason: <thorough explanation>\nApproaches tried: <list>\nSuggestions for follow-up: <if any>")

5. **Use telegram_send** to share key findings with the user mid-research.

Once you have enough information, respond with your final summary and recommendations without making further tool calls.
When editing project files, use the filesystem tools (read_file, write_file, search_replace_file, etc.) with the task's project_id so files live in that project's workspace. Prefer search_replace_file for targeted edits; if it reports the old string was not found, use write_file to replace the whole file.

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


def _create_llm():
    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError(
            "OPENROUTER_API_KEY (or OPENAI_API_KEY) must be set. Add it to .env or export it."
        )
    return ChatOpenRouter(
        model=os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v3.2"),
        temperature=0,
        api_key=api_key,
        timeout=LLM_TIMEOUT * 1000,  # SDK expects milliseconds
    )


def _system_prompt(max_tool_rounds: int) -> str:
    """Build system prompt; nudge to limit page visits when not in deep research mode."""
    prompt = SYSTEM_PROMPT_BASE
    if max_tool_rounds <= DEFAULT_MAX_TOOL_ROUNDS:
        prompt += f"\nUse at most {max_tool_rounds} tool calls (page visits); then summarize your answer without further tool use."
    return prompt


def _llm_call(state: MessagesState) -> dict:
    """LLM node: invoke model with tools and return the response message."""
    try:
        run_config = get_config()
        max_rounds = (run_config.get("configurable") or {}).get("max_tool_rounds") or DEFAULT_MAX_TOOL_ROUNDS
    except RuntimeError:
        max_rounds = DEFAULT_MAX_TOOL_ROUNDS
    system_content = _system_prompt(max_rounds)
    messages = [SystemMessage(content=system_content)] + list(state["messages"])
    total_chars = sum(len(str(getattr(m, "content", ""))) for m in messages)
    logger.info(
        "Calling LLM (OpenRouter) — messages=%d, context_chars=%d, timeout=%ds",
        len(messages),
        total_chars,
        LLM_TIMEOUT,
    )
    llm = _create_llm()
    llm_with_tools = llm.bind_tools(TOOLS) if TOOLS else llm
    t0 = time.monotonic()
    response = llm_with_tools.invoke(messages)
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
        if tool is None:
            observation = f"Unknown tool: {name}"
            logger.warning("Unknown tool: %s", name)
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
        # Auto-save to memory after research tools (plugin has "research" tag)
        if "research" in TOOL_PLUGIN_TAGS.get(name, []):
            obs_str = str(observation).strip()
            skip_error_prefixes = ("Tool error", "[memory_rag ERROR]", "[browser_research ERROR]")
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
        result.append(ToolMessage(content=str(observation), tool_call_id=call_id))
        _cb = getattr(_thread_local, "on_progress", None)
        if _cb:
            try:
                _cb(name, args, str(observation))
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
    """Build and return a compiled LangGraph agent with tool-calling and browser research."""
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
    llm = _create_llm()
    response = llm.invoke([HumanMessage(content=summary_prompt)])
    result = getattr(response, "content", str(response)) or ""
    logger.info("Fallback summary length=%d", len(result))
    return f"⚠️ Hit tool-call limit; here's a summary of what was found:\n\n{result}"


def run_research(query: str, on_progress=None) -> str:
    """Run the research agent on a query and return the final assistant message content."""
    _thread_local.on_progress = on_progress
    deep = _is_deep_research(query)
    max_rounds = DEEP_RESEARCH_MAX_TOOL_ROUNDS if deep else DEFAULT_MAX_TOOL_ROUNDS
    logger.info("Invoking research agent... (max_tool_rounds=%s, deep=%s)", max_rounds, deep)
    agent = create_research_agent()
    config = {
        "recursion_limit": _recursion_limit(max_rounds),
        "configurable": {"max_tool_rounds": max_rounds},
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
